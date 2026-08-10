"""Manually smoke-test the generic JSONL entity engine."""

import importlib
import json
import sys
import tempfile
import threading
from pathlib import Path

from healthcare_test_data.config import EntityConfig
from healthcare_test_data.engine import run_entity


def main() -> None:
    """Exercise generic streaming, atomic publishing, and failure cleanup.

    Raises:
        AssertionError: If any generic engine safety or determinism contract
            regresses.
    """
    with tempfile.TemporaryDirectory() as temporary_directory:
        workspace = Path(temporary_directory)
        package_name = "generic_smoke_entity"
        _write_package(workspace, package_name)
        sys.path.insert(0, str(workspace))
        try:
            _assert_streaming_and_determinism(workspace, package_name)
            _assert_invalid_records_are_not_published(workspace, package_name)
            _assert_concurrent_cleanup_is_isolated(workspace, package_name)
            _assert_import_failure_is_not_published(workspace)
            _assert_resolved_output_escape_is_rejected(workspace, package_name)
        finally:
            sys.path.remove(str(workspace))
            _remove_modules(package_name)


def _write_package(workspace: Path, package_name: str) -> None:
    """Create a temporary provider-neutral entity package and schema.

    Args:
        workspace: Temporary directory that owns the generated smoke package.
        package_name: Importable package name to create.
    """
    package = workspace / package_name
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "records.py").write_text(
        '"""Temporary generic smoke entity."""\n\n'
        "last_validated = -1\n"
        "\n"
        "class ValidatedRecord(dict[str, object]):\n"
        '    """Mark a record when validation reads its payload."""\n'
        "\n"
        "    def __getitem__(self, key: str) -> object:\n"
        '        """Return a value and record schema validation progress."""\n'
        "        global last_validated\n"
        "        value = super().__getitem__(key)\n"
        "        if key == 'index':\n"
        "            last_validated = int(value)\n"
        "        return value\n"
        "\n"
        "def generate_record(seed: int, index: int) -> dict[str, object]:\n"
        '    """Generate a record only after the preceding one was validated."""\n'
        "    if index and last_validated != index - 1:\n"
        "        raise RuntimeError('records were requested ahead of validation')\n"
        "    return ValidatedRecord({'seed': seed, 'index': index})\n",
        encoding="utf-8",
    )
    (workspace / "record.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["seed", "index"],
                "properties": {
                    "seed": {"type": "integer"},
                    "index": {"type": "integer", "minimum": 0},
                },
            }
        ),
        encoding="utf-8",
    )
    (package / "blocking.py").write_text(
        '"""Temporary entity that pauses after creating its output file."""\n\n'
        "started = None\n"
        "release = None\n"
        "\n"
        "def generate_record(seed: int, index: int) -> dict[str, object]:\n"
        '    """Wait for the smoke to exercise concurrent cleanup."""\n'
        "    if started is None or release is None:\n"
        "        raise RuntimeError('smoke synchronization is not configured')\n"
        "    started.set()\n"
        "    if not release.wait(timeout=5):\n"
        "        raise RuntimeError('smoke synchronization timed out')\n"
        "    return {'seed': seed, 'index': index}\n",
        encoding="utf-8",
    )


def _assert_streaming_and_determinism(workspace: Path, package_name: str) -> None:
    """Prove bounded record handling, line count, and stable repeated output.

    Args:
        workspace: Temporary directory containing the smoke fixtures.
        package_name: Importable package containing the generic record module.

    Raises:
        AssertionError: If output does not stream, has the wrong count, or is
            not deterministic.
    """
    entity = EntityConfig(
        name="generic",
        count=3,
        scenarios={},
        profile="generic",
        schema=workspace / "record.schema.json",
        module=f"{package_name}.records",
        filename="generic.jsonl",
    )
    output_directory = workspace / "output"
    first_path = run_entity(entity, seed=41, output_directory=output_directory)
    first_bytes = first_path.read_bytes()
    assert len(first_bytes.splitlines()) == entity.count

    records_module = importlib.import_module(entity.module)
    records_module.last_validated = -1
    second_path = run_entity(entity, seed=41, output_directory=output_directory)
    assert second_path.read_bytes() == first_bytes


def _assert_invalid_records_are_not_published(workspace: Path, package_name: str) -> None:
    """Prove validation failures publish neither new nor replacement files.

    Args:
        workspace: Temporary directory containing the smoke fixtures.
        package_name: Package where the invalid entity is created.

    Raises:
        AssertionError: If invalid data publishes or replaces output.
    """
    invalid_module = workspace / package_name / "invalid.py"
    invalid_module.write_text(
        '"""Temporary invalid smoke entity."""\n\n'
        "def generate_record(seed: int, index: int) -> dict[str, object]:\n"
        '    """Return a deliberately invalid generic record."""\n'
        "    return {'seed': 'not-an-integer', 'index': index}\n",
        encoding="utf-8",
    )
    entity = EntityConfig(
        name="invalid",
        count=1,
        scenarios={},
        profile="generic",
        schema=workspace / "record.schema.json",
        module=f"{package_name}.invalid",
        filename="invalid.jsonl",
    )
    output_directory = workspace / "invalid-output"
    try:
        run_entity(entity, seed=41, output_directory=output_directory)
    except Exception:
        pass
    else:
        raise AssertionError("An invalid record unexpectedly published output")
    assert not (output_directory / entity.filename).exists()
    assert not tuple(output_directory.glob(f".{entity.filename}.*.tmp"))

    preserved_path = output_directory / "preserved.jsonl"
    output_directory.mkdir(parents=True, exist_ok=True)
    preserved_path.write_bytes(b"existing output\n")
    preserved_entity = EntityConfig(
        name="invalid",
        count=1,
        scenarios={},
        profile="generic",
        schema=entity.schema,
        module=entity.module,
        filename=preserved_path.name,
    )
    try:
        run_entity(preserved_entity, seed=41, output_directory=output_directory)
    except Exception:
        pass
    else:
        raise AssertionError("An invalid record unexpectedly replaced output")
    assert preserved_path.read_bytes() == b"existing output\n"


def _assert_concurrent_cleanup_is_isolated(workspace: Path, package_name: str) -> None:
    """Prove one failed invocation cannot remove another run's temp file.

    Args:
        workspace: Temporary directory containing the smoke fixtures.
        package_name: Package containing the blocking and invalid entities.

    Raises:
        AssertionError: If concurrent cleanup removes active work or produces
            an unexpected final output.
    """
    output_directory = workspace / "concurrent-output"
    filename = "shared.jsonl"
    schema = workspace / "record.schema.json"
    blocking_entity = EntityConfig(
        name="blocking",
        count=1,
        scenarios={},
        profile="generic",
        schema=schema,
        module=f"{package_name}.blocking",
        filename=filename,
    )
    invalid_entity = EntityConfig(
        name="invalid",
        count=1,
        scenarios={},
        profile="generic",
        schema=schema,
        module=f"{package_name}.invalid",
        filename=filename,
    )
    blocking_module = importlib.import_module(blocking_entity.module)
    blocking_module.started = threading.Event()
    blocking_module.release = threading.Event()
    failures: list[BaseException] = []

    def run_blocking_entity() -> None:
        """Run the successful entity while its temporary file remains active."""
        try:
            run_entity(blocking_entity, seed=41, output_directory=output_directory)
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=run_blocking_entity)
    thread.start()
    assert blocking_module.started.wait(timeout=2)
    active_temporary_paths = tuple(output_directory.glob(f".{filename}.*.tmp"))
    assert len(active_temporary_paths) == 1
    active_temporary_path = active_temporary_paths[0]

    try:
        run_entity(invalid_entity, seed=41, output_directory=output_directory)
    except Exception:
        pass
    else:
        raise AssertionError("An invalid concurrent entity unexpectedly published output")

    assert active_temporary_path.exists()
    assert tuple(output_directory.glob(f".{filename}.*.tmp")) == (active_temporary_path,)
    assert not (output_directory / filename).exists()
    blocking_module.release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert not failures
    assert (output_directory / filename).exists()
    assert not tuple(output_directory.glob(f".{filename}.*.tmp"))


def _assert_import_failure_is_not_published(workspace: Path) -> None:
    """Prove an unavailable entity module does not create final output.

    Args:
        workspace: Temporary directory containing the smoke schema.

    Raises:
        AssertionError: If a failed import publishes output.
    """
    entity = EntityConfig(
        name="missing",
        count=1,
        scenarios={},
        profile="generic",
        schema=workspace / "record.schema.json",
        module="missing_generic_smoke_entity",
        filename="missing.jsonl",
    )
    output_directory = workspace / "missing-output"
    try:
        run_entity(entity, seed=41, output_directory=output_directory)
    except RuntimeError:
        pass
    else:
        raise AssertionError("A missing module unexpectedly published output")
    assert not (output_directory / entity.filename).exists()
    assert not tuple(output_directory.glob(f".{entity.filename}.*.tmp"))


def _assert_resolved_output_escape_is_rejected(workspace: Path, package_name: str) -> None:
    """Prove a filename cannot escape through an output-directory symlink.

    Args:
        workspace: Temporary directory where the symlink fixture is created.
        package_name: Package containing the valid generic entity.

    Raises:
        AssertionError: If a resolved filename escapes its output directory.
    """
    output_directory = workspace / "safe-output"
    outside_directory = workspace / "outside-output"
    output_directory.mkdir()
    outside_directory.mkdir()
    (output_directory / "escape").symlink_to(outside_directory, target_is_directory=True)
    entity = EntityConfig(
        name="escape",
        count=1,
        scenarios={},
        profile="generic",
        schema=workspace / "record.schema.json",
        module=f"{package_name}.records",
        filename="escape/providers.jsonl",
    )
    try:
        run_entity(entity, seed=41, output_directory=output_directory)
    except RuntimeError as error:
        assert "resolves outside the output directory" in str(error)
        assert entity.filename not in str(error)
    else:
        raise AssertionError("A resolved output escape unexpectedly succeeded")
    assert not (outside_directory / "providers.jsonl").exists()


def _remove_modules(package_name: str) -> None:
    """Remove temporary smoke modules from the interpreter cache.

    Args:
        package_name: Root temporary package name to evict.
    """
    for module_name in tuple(sys.modules):
        if module_name == package_name or module_name.startswith(f"{package_name}."):
            del sys.modules[module_name]


if __name__ == "__main__":
    main()
