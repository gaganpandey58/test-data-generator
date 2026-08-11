"""Coordinate configuration loading and JSONL generation for command-line use.

This module is the intentionally small public boundary around the generator.
It translates domain-specific failures into concise messages that are safe to
show in a terminal, while the configuration and engine modules retain the
details of parsing, validation, and atomic file publication.
"""

import argparse
import sys
from pathlib import Path

from healthcare_test_data.config import RunConfig, load_config
from healthcare_test_data.engine import run_entity
from healthcare_test_data.errors import ConfigurationError, GenerationError


class CommandError(RuntimeError):
    """Represent an expected command failure that is safe to display.

    The CLI raises this error only after adding contextual information to a
    configuration or generation failure.  :func:`main` catches it and returns
    the documented non-zero command exit status instead of exposing a stack
    trace to the user.
    """


def generate(config: Path) -> None:
    """Generate every enabled entity described by one configuration file.

    The function loads and validates the supplied configuration once, passes
    shared entity-count context to each enabled generator, prints
    the resulting JSONL path, and removes only stale output files belonging to
    disabled known entities.

    Args:
        config: Path to the root JSON generation configuration.

    Raises:
        CommandError: If configuration loading or entity generation fails.
    """
    try:
        run_config = load_config(config)
    except ConfigurationError as error:
        raise CommandError(f"Configuration failed for {config.resolve()}: {error}") from error

    entity_counts = {entity.name: entity.count for entity in run_config.entities}
    for entity in run_config.entities:
        try:
            output_path = run_entity(
                entity,
                run_config.seed,
                run_config.output_directory,
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
    _remove_disabled_outputs(run_config)


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
        path = output_directory / filename
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
    arguments = parser.parse_args()

    try:
        generate(arguments.config)
    except CommandError as error:
        print(error, file=sys.stderr)
        return 2
    except Exception:
        print("Generation failed", file=sys.stderr)
        return 2
    return 0
