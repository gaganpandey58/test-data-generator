"""Coordinate configuration loading and JSONL generation for command-line use.

This module is the intentionally small public boundary around the generator.
It translates domain-specific failures into concise messages that are safe to
show in a terminal, while the configuration and engine modules retain the
details of parsing, validation, and atomic file publication.
"""

import argparse
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from test_data_generator.configuration.config import RunConfig, load_config
from test_data_generator.core.engine import run_entity, run_update_entity
from test_data_generator.core.errors import ConfigurationError, GenerationError
from test_data_generator.provider_cdf import (
    generate_linked_provider_fixtures,
    generate_nppes_file,
    generate_provider_cdf,
)
from test_data_generator.update.rules import load_rule_catalog
from test_data_generator.update.scenarios import UpdateRequest, UpdateScenario


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

    entity_counts = {entity.name: entity.count for entity in run_config.entities}
    if mode in {"all", "creation"}:
        for entity in run_config.entities:
            if (
                entity.name == "provider"
                and run_config.provider_linked
                and run_config.nppes_count > 0
            ):
                assert run_config.nppes_sample is not None
                try:
                    paths = generate_linked_provider_fixtures(
                        run_config.nppes_sample,
                        run_config.creation_directory,
                        run_config.nppes_count,
                        entity.count - run_config.nppes_count,
                        run_config.seed,
                        entity.client_headers,
                        entity.client_values,
                    )
                except (OSError, ValueError) as error:
                    raise CommandError(f"Linked provider generation failed: {error}") from error
                print(f"provider: {entity.count} records -> {paths['provider_cdf']}")
                print(
                    f"provider_nppes: {run_config.nppes_count} records -> {paths['provider_nppes']}"
                )
                continue
            try:
                output_path = run_entity(
                    entity,
                    run_config.seed,
                    run_config.creation_directory,
                    entity_counts,
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
            print(f"{entity.name}: {entity.count} records -> {output_path}")
        if run_config.nppes_count > 0 and not run_config.provider_linked:
            assert run_config.nppes_sample is not None
            try:
                nppes_path = generate_nppes_file(
                    run_config.nppes_sample,
                    run_config.creation_directory / run_config.nppes_filename,
                    run_config.nppes_count,
                    run_config.seed,
                )
            except (OSError, ValueError) as error:
                raise CommandError(f"NPPES generation failed: {error}") from error
            print(f"provider_nppes: {run_config.nppes_count} records -> {nppes_path}")
    if mode in {"all", "updates"} and run_config.updates_enabled:
        assert rules is not None
        for entity in run_config.entities:
            entity_rules = rules.get(entity.name)
            if entity_rules is None:
                raise CommandError(f"Update rule catalog has no rules for {entity.name!r}")
            request = _update_request(run_config, entity)
            try:
                output_path = run_update_entity(
                    entity,
                    run_config.seed,
                    run_config.update_directory,
                    entity_counts,
                    request,
                    entity_rules,
                )
            except (GenerationError, ValueError) as error:
                raise CommandError(
                    f"Update generation failed for entity {entity.name!r}: {error}"
                ) from error
            print(f"{entity.name}: {entity.count} updates -> {output_path}")
    _remove_disabled_outputs(run_config)


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
    scenario_value = str(raw.get("scenario", raw.get("default_scenario", "UPDATE_SINGLE_FIELD")))
    try:
        scenario = UpdateScenario(scenario_value)
    except ValueError as error:
        raise CommandError(f"Unknown update scenario {scenario_value!r}") from error
    fields = _string_tuple(raw, "fields")
    include = _string_tuple(raw, "include")
    exclude = _string_tuple(raw, "exclude")
    threshold = raw.get("threshold")
    try:
        parsed_threshold = Decimal(str(threshold)) if threshold is not None else None
    except (InvalidOperation, ValueError) as error:
        raise CommandError("Update threshold must be a decimal number") from error
    return UpdateRequest(
        scenario=scenario,
        fields=fields,
        include=include,
        exclude=exclude,
        matching_method=str(raw["matching_method"]) if "matching_method" in raw else None,
        threshold=parsed_threshold,
    )


def _string_tuple(values: dict[str, object], key: str) -> tuple[str, ...]:
    """Read an optional string-list setting from normalized configuration."""
    value = values.get(key, ())
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return ()
    return tuple(value)


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
        "provider-cdf", help="Generate NPPES, provider CDF, and updated CDF fixtures."
    )
    provider_cdf_parser.add_argument("--sample-nppes", required=True, type=Path)
    provider_cdf_parser.add_argument("--output", required=True, type=Path)
    provider_cdf_parser.add_argument("--count", type=int, default=10)
    provider_cdf_parser.add_argument("--unmatched-count", type=int, default=2)
    provider_cdf_parser.add_argument("--seed", type=int, default=20260805)
    arguments = parser.parse_args()

    try:
        if arguments.command == "provider-cdf":
            paths = generate_provider_cdf(
                arguments.sample_nppes,
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
