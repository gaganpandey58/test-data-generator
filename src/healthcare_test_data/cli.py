"""Command-line entry point for simple JSONL data generation."""

import argparse
import sys
from pathlib import Path

from healthcare_test_data.config import RunConfig, load_config
from healthcare_test_data.engine import run_entity
from healthcare_test_data.errors import ConfigurationError, GenerationError


class CommandError(RuntimeError):
    """Represent a CLI failure that is safe to present to users."""


def generate(config: Path) -> None:
    """Generate every enabled entity from one configuration file.

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
    entity_scenarios = {entity.name: entity.scenarios for entity in run_config.entities}
    for entity in run_config.entities:
        try:
            output_path = run_entity(
                entity,
                run_config.seed,
                run_config.output_directory,
                entity_counts,
                entity_scenarios,
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
    considered. Unrelated files in the configured output directory remain
    untouched.

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
    """Run the supported generation command.

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
