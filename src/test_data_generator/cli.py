"""Coordinate configuration loading and JSONL generation for command-line use.

This module is the intentionally small public boundary around the generator.
It translates domain-specific failures into concise messages that are safe to
show in a terminal, while the configuration and engine modules retain the
details of parsing, validation, and atomic file publication.
"""

import argparse
import json
import shutil
import sys
import tempfile
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

from test_data_generator.configuration.config import (
    RunConfig,
    load_config,
    load_execution_config,
    resolve_execution_mode,
    select_execution_entities,
)
from test_data_generator.core.engine import (
    build_claim_pair_records,
    build_entity_records,
    build_related_records,
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
from test_data_generator.entities.provider_nppes import generate_records as generate_nppes_records
from test_data_generator.update.match_fixtures import generate_match_fixture_matrix
from test_data_generator.update.rules import (
    extend_rules_for_records,
    load_rule_catalog,
    rules_for_records,
)
from test_data_generator.update.scenarios import (
    ExpectedOutcome,
    FailureMode,
    FieldModification,
    OperationType,
    UpdateRequest,
    load_invalid_values,
    may_violate_schema,
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
    try:
        execution = load_execution_config(config)
        run_config = select_execution_entities(load_config(execution.config_path), execution)
        mode = resolve_execution_mode(mode, execution)
    except ConfigurationError as error:
        raise CommandError(f"Configuration failed for {config.resolve()}: {error}") from error

    rules = None
    needs_match_fixtures = (
        mode in {"all", "creation"}
        and run_config.creation_enabled
        and bool(run_config.match_fixture_entities)
    )
    if (mode in {"all", "updates"} and run_config.updates_enabled) or needs_match_fixtures:
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
            and entity.linked_to_claim
        }
        for entity in run_config.entities:
            if entity.name in histories:
                continue
            if entity.count == 0:
                continue
            if entity.name == "provider_nppes":
                if run_config.provider_linked:
                    # The CDF-linked provider branch below writes the shared
                    # source NPPES stream and registers its rows.
                    continue
                try:
                    nppes_path = generate_nppes_file(
                        run_config.creation_directory / entity.filename,
                        entity.count,
                        run_config.seed,
                        run_config.nppes_individual_count,
                        run_config.nppes_organizational_count,
                    )
                except (OSError, ValueError) as error:
                    raise CommandError(f"NPPES generation failed: {error}") from error
                print(
                    f"{entity.name}: {entity.count} records -> {transaction.final_path(nppes_path)}"
                )
                generated_records[entity.name] = _read_jsonl_records(nppes_path)
                continue
            if entity.source_entity is not None:
                source_records = generated_records.get(entity.source_entity)
                if source_records is None:
                    raise CommandError(
                        f"Related entity {entity.name!r} requires source entity "
                        f"{entity.source_entity!r}"
                    )
                try:
                    records = build_related_records(entity, source_records)
                    output_path = run_records(entity, records, run_config.creation_directory)
                except GenerationError as error:
                    raise CommandError(
                        f"Related generation failed for entity {entity.name!r}: {error}"
                    ) from error
                print(
                    f"{entity.name}: {entity.count} records -> "
                    f"{transaction.final_path(output_path)}"
                )
                generated_records[entity.name] = _read_jsonl_records(output_path)
                continue
            history_entity_name = {
                "claim_professional": "claim_history_professional",
                "claim_institutional": "claim_history_institutional",
            }.get(entity.name)
            if history_entity_name is not None:
                history_entity = histories.get(history_entity_name)
                if history_entity is not None:
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
                        f"{entity.name}: {entity.count} records -> "
                        f"{transaction.final_path(claim_path)}"
                    )
                    print(
                        f"{history_entity.name}: {history_entity.count} records -> "
                        f"{transaction.final_path(history_path)}"
                    )
                    generated_records[entity.name] = _read_jsonl_records(claim_path)
                    generated_records[history_entity.name] = _read_jsonl_records(history_path)
                    continue
            claim_source_name = _payment_claim_source_name(entity.name, generated_records)
            if claim_source_name:
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
                generated_records["provider_nppes"] = _read_jsonl_records(paths["provider_nppes"])
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
            nppes_path = run_config.creation_directory / run_config.nppes_filename
            if "provider_nppes" not in generated_records:
                try:
                    nppes_path = generate_nppes_file(
                        nppes_path,
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
                generated_records["provider_nppes"] = _read_jsonl_records(nppes_path)
        if needs_match_fixtures:
            assert rules is not None
            try:
                _materialize_match_fixture_bases(run_config, entity_counts, generated_records)
                fixture_paths = generate_match_fixture_matrix(
                    run_config.match_fixture_entities,
                    generated_records,
                    rules,
                    run_config.seed,
                    run_config.update_directory,
                    run_config.invalid_values_catalog,
                )
            except ValueError as error:
                raise CommandError(f"Match-fixture generation failed: {error}") from error
            print(
                f"match fixtures: {len(fixture_paths)} files -> "
                f"{transaction.final_path(run_config.update_directory)}"
            )
    if mode in {"all", "updates"} and run_config.updates_enabled:
        assert rules is not None
        _materialize_update_bases(run_config, entity_counts, generated_records)
        entities_by_name = {entity.name: entity for entity in run_config.entities}
        propagated_payment_updates: set[str] = set()
        for entity in run_config.entities:
            if entity.count == 0:
                continue
            if (
                entity.name
                in {
                    "claim_history_professional",
                    "claim_history_institutional",
                }
                and entity.linked_to_claim
            ):
                continue
            # Update generation is opt-in per stream. A global enabled flag
            # permits updates; it does not manufacture a default mutation for
            # every created entity. Related streams can still be propagated
            # from an explicitly updated Claim below.
            has_explicit_update = bool(
                {"operation", "expected_outcome", "modifications"}.intersection(entity.update)
            )
            if not has_explicit_update:
                continue
            rules_entity = entity.source_entity or entity.name
            entity_rules = rules.get(rules_entity)
            if entity_rules is not None and entity_rules.allow_absent_fields:
                entity_rules = extend_rules_for_records(
                    entity_rules, generated_records.get(entity.name, ())
                )
            if entity_rules is None and entity.name == "provider_nppes":
                bases = generated_records.get(entity.name, ())
                entity_rules = rules_for_records(
                    entity.name,
                    entity.profile,
                    bases,
                    keys=("NPI",),
                )
            if entity_rules is None:
                raise CommandError(f"Update rule catalog has no rules for {rules_entity!r}")
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
                elif claim_source_name := _payment_claim_source_name(
                    entity.name, generated_records
                ):
                    records = derive_payments_from_records(
                        generated_records[claim_source_name],
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
                if history_entity is not None and history_entity.linked_to_claim:
                    try:
                        history_bases = generated_records[history_name]
                        updated_claims = _read_jsonl_records(output_path)
                        changed_claim_fields = tuple(
                            _changed_field_names(base, updated)
                            for base, updated in zip(
                                generated_records[entity.name], updated_claims, strict=True
                            )
                        )
                        updated_history = _derive_claim_history_updates(
                            updated_claims,
                            history_bases,
                            changed_claim_fields,
                            _history_identifier_values(request),
                        )
                        schema_invalid_match_fixture = (
                            request.expected_outcome == ExpectedOutcome.NO_MATCH
                            and request.failure_mode
                            in {FailureMode.INVALID_VALUE, FailureMode.MISSING_VALUE}
                        )
                        history_path = run_derived_update_records(
                            history_entity,
                            updated_history,
                            run_config.update_directory,
                            validate_schema=not schema_invalid_match_fixture
                            and not may_violate_schema(request),
                        )
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
                            validate_schema=not schema_invalid_match_fixture
                            and not may_violate_schema(request),
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
        _remove_unrequested_related_updates(run_config)
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
    if run_config.match_fixture_entities and mode in {"all", "creation"}:
        _clear_match_fixture_directories(staged_updates, run_config)
    staged_config = replace(
        run_config,
        output_directory=temporary_root / "legacy",
        creation_directory=staged_creation,
        update_directory=staged_updates,
    )
    pairs: list[tuple[Path, Path]] = []
    if mode in {"all", "creation"} and run_config.creation_enabled:
        pairs.append((staged_creation, run_config.creation_directory))
    if (mode in {"all", "updates"} and run_config.updates_enabled) or (
        mode in {"all", "creation"}
        and run_config.creation_enabled
        and run_config.match_fixture_entities
    ):
        pairs.append((staged_updates, run_config.update_directory))
    return _OutputTransaction(temporary, staged_config, tuple(pairs))


def _clear_match_fixture_directories(directory: Path, run_config: RunConfig) -> None:
    """Remove only prior match-code folders for the configured source streams."""
    fixture_root = directory / "match-fixtures"
    if fixture_root.is_dir():
        shutil.rmtree(fixture_root)
    for fixture in run_config.match_fixture_entities:
        for candidate in directory.glob(f"{fixture.entity}[0-9]*"):
            if candidate.is_dir() and candidate.name.removeprefix(fixture.entity).isdigit():
                shutil.rmtree(candidate)


def _materialize_match_fixture_bases(
    run_config: RunConfig,
    entity_counts: Mapping[str, int],
    generated_records: dict[str, tuple[Mapping[str, object], ...]],
) -> None:
    """Build in-memory source rows for fixture-only, zero-count streams."""
    entities = {entity.name: entity for entity in run_config.entities}
    for fixture in run_config.match_fixture_entities:
        if generated_records.get(fixture.entity):
            continue
        entity = entities.get(fixture.entity)
        if entity is None:
            raise CommandError(
                f"Match-fixture entity {fixture.entity!r} has no resolved stream configuration"
            )
        if entity.name == "provider_nppes":
            generated_records[entity.name] = tuple(generate_nppes_records(2, run_config.seed, 1, 1))
            continue
        fixture_entity = replace(entity, count=1, source_entity=None)
        try:
            generated_records[entity.name] = tuple(
                build_entity_records(
                    fixture_entity,
                    run_config.seed,
                    {**entity_counts, entity.name: 1},
                    generated_records,
                )
            )
        except (GenerationError, ValueError) as error:
            raise CommandError(
                f"Unable to build match-fixture source for {entity.name!r}: {error}"
            ) from error


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


_CLAIM_HISTORY_IDENTIFIER_FIELDS = (
    "CH_CLIENT_CLAIM_UNIQUE_ID",
    "CH_CLIENT_CLAIM_ID",
    "CH_CLIENT_ORIGINAL_CLAIM_ID",
)


def _derive_claim_history_updates(
    updated_claims: tuple[Mapping[str, object], ...],
    history_bases: tuple[Mapping[str, object], ...],
    changed_fields: tuple[frozenset[str], ...],
    identifier_values: Mapping[str, object] | None = None,
) -> tuple[Mapping[str, object], ...]:
    """Derive CH updates from their corresponding 837 updates exactly.

    Claims History is the paired representation of a Claim, not a second
    independently-randomized update.  History preserves its populated client
    identifiers, unless the claim update explicitly supplied a replacement
    value.  837 output intentionally blanks these client identifiers; CH must
    nevertheless always keep a populated value and its own discriminator.
    """
    if len(updated_claims) != len(history_bases) or len(updated_claims) != len(changed_fields):
        raise CommandError("Claim and Claims History update record counts differ")
    records: list[Mapping[str, object]] = []
    for claim, history_base, _changed in zip(
        updated_claims, history_bases, changed_fields, strict=True
    ):
        history = deepcopy(dict(claim))
        for field in _CLAIM_HISTORY_IDENTIFIER_FIELDS:
            if identifier_values is not None and field in identifier_values:
                history[field] = deepcopy(identifier_values[field])
            elif field in history_base:
                history[field] = history_base[field]
        history["FILE_TYPE"] = "CH"
        records.append(history)
    return tuple(records)


def _history_identifier_values(request: UpdateRequest) -> Mapping[str, object]:
    """Return explicitly configured valid CH identifier replacements only.

    Claims layouts deliberately hide their client claim IDs.  A configured
    value is therefore the sole reliable source for propagating an intentional
    ID change to the linked CH record; absent one, the established CH ID is
    preserved instead of fabricating a random replacement.
    """
    configured: dict[str, object] = {}
    for values in (
        request.values,
        *(modification.values for modification in request.modifications),
    ):
        if values is None:
            continue
        for field in _CLAIM_HISTORY_IDENTIFIER_FIELDS:
            if field in values:
                configured[field] = deepcopy(values[field])
    return configured


def _payment_claim_source_name(name: str, generated_records: Mapping[str, object]) -> str:
    """Return the preferred in-run History or Claim source for a Payment stream."""
    candidates = {
        "payment_professional": (
            "claim_history_professional",
            "claim_professional",
        ),
        "payment_institutional": (
            "claim_history_institutional",
            "claim_institutional",
        ),
    }.get(name, ())
    return next((candidate for candidate in candidates if candidate in generated_records), "")


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
        and entity.linked_to_claim
    }
    for entity in run_config.entities:
        if entity.name in generated_records or entity.name in histories:
            continue
        if entity.name == "provider_nppes":
            if run_config.provider_linked:
                # The provider branch builds the linked CDF/NPPES pair.
                continue
            generated_records[entity.name] = tuple(
                generate_nppes_records(
                    entity.count,
                    run_config.seed,
                    run_config.nppes_individual_count,
                    run_config.nppes_organizational_count,
                )
            )
            continue
        if entity.source_entity is not None:
            source_records = generated_records.get(entity.source_entity)
            if source_records is None:
                raise CommandError(
                    f"Related entity {entity.name!r} requires source entity "
                    f"{entity.source_entity!r}"
                )
            generated_records[entity.name] = tuple(build_related_records(entity, source_records))
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
            generated_records["provider_nppes"] = tuple(records["provider_nppes"])
            continue
        history_name = {
            "claim_professional": "claim_history_professional",
            "claim_institutional": "claim_history_institutional",
        }.get(entity.name)
        if history_name is not None:
            history_entity = histories.get(history_name)
            if history_entity is not None:
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
    expected_outcome_value = raw.get("expected_outcome")
    try:
        expected_outcome = (
            ExpectedOutcome(str(expected_outcome_value))
            if expected_outcome_value is not None
            else None
        )
        failure_mode = FailureMode(str(raw["failure_mode"])) if "failure_mode" in raw else None
    except ValueError as error:
        raise CommandError("Unknown expected_outcome or failure_mode") from error
    operation_config = raw.get("operation")
    if operation_config is None and expected_outcome is not None:
        operation_config = {"type": OperationType.DUPLICATE.value}
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
    modifications = _field_modifications(raw)
    needs_invalid_catalog = (
        operation_type == OperationType.INVALID
        or any(modification.operation == OperationType.INVALID for modification in modifications)
        or failure_mode == FailureMode.INVALID_VALUE
    )
    return UpdateRequest(
        fields=fields,
        include=include,
        exclude=exclude,
        matching_method=str(raw["matching_method"]) if "matching_method" in raw else None,
        threshold=parsed_threshold,
        operation=operation_type,
        condition=operation_condition,
        values=_field_values(operation_config, "operation"),
        invalid_values=(
            load_invalid_values(run_config.invalid_values_catalog)
            if needs_invalid_catalog and run_config.invalid_values_catalog is not None
            else None
        ),
        expected_outcome=expected_outcome,
        failure_mode=failure_mode,
        failure_field=str(raw["failure_field"]) if "failure_field" in raw else None,
        collision_method=str(raw["collision_method"]) if "collision_method" in raw else None,
        elasticity_boundary=(
            str(raw["elasticity_boundary"]) if "elasticity_boundary" in raw else None
        ),
        modifications=modifications,
    )


def _field_modifications(raw: Mapping[str, object]) -> tuple[FieldModification, ...]:
    """Normalize independently selectable field operations for one run."""
    definitions = raw.get("modifications", [])
    if not isinstance(definitions, list):
        raise CommandError("modifications must be an array")
    result: list[FieldModification] = []
    for definition in definitions:
        if not isinstance(definition, Mapping):
            raise CommandError("Each modification must be an object")
        try:
            operation = OperationType(str(definition.get("type", "")))
        except ValueError as error:
            raise CommandError("Unknown modification operation") from error
        condition = str(definition["condition"]) if "condition" in definition else None
        if operation == OperationType.WEIGHT_CHANGE and condition is None:
            raise CommandError("WEIGHT_CHANGE modification requires a condition")
        result.append(
            FieldModification(
                operation,
                _string_tuple(definition, "fields"),
                condition,
                _field_values(definition, "modification"),
            )
        )
    return tuple(result)


def _field_values(values: Mapping[str, object], label: str) -> Mapping[str, object] | None:
    """Read exact field replacements for a deterministic UPDATE operation."""
    configured = values.get("values")
    if configured is None:
        return None
    if not isinstance(configured, Mapping) or not all(
        isinstance(name, str) and name.strip() for name in configured
    ):
        raise CommandError(f"{label}.values must be an object keyed by field name")
    return {name: deepcopy(value) for name, value in configured.items()}


def _string_tuple(values: Mapping[str, object], key: str) -> tuple[str, ...]:
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


def _remove_unrequested_related_updates(run_config: RunConfig) -> None:
    """Remove stale related-stream updates unless that stream requests one."""
    for entity in run_config.entities:
        if entity.source_entity is None or "operation" in entity.update:
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
    enabled_filenames = {entity.filename for entity in run_config.entities if entity.count > 0}
    # NPPES has a code-defined source shape and supports a creation-only
    # shortcut when it has no update operation.  That shortcut deliberately
    # does not add a generic EntityConfig, but its emitted file is still an
    # enabled output and must not be removed as stale.
    if run_config.nppes_count > 0:
        enabled_filenames.add(run_config.nppes_filename)
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
    """Generate using the repository's standard ``runconfig.json`` file.

    This is the short console command installed as ``generate-data``. It keeps
    normal use to one command while ``main`` remains available for an optional
    alternate configuration path.
    """
    try:
        generate(Path("runconfig.json"))
    except CommandError as error:
        print(error, file=sys.stderr)
        return 2
    return 0
