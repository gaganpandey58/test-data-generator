"""Exercise the simple generation configuration seam without creating output."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from healthcare_test_data.config import load_config
from healthcare_test_data.errors import ConfigurationError


def main() -> None:
    """Verify root loading and unsafe or incomplete configuration rejection.

    Raises:
        AssertionError: If configuration loading accepts missing schemas or
            unsafe output filenames.
    """
    repository_root = Path(__file__).resolve().parents[1]
    with TemporaryDirectory() as temporary_directory:
        previous_directory = Path.cwd()
        try:
            os.chdir(temporary_directory)
            config = load_config(repository_root / "generator.config.json")
        finally:
            os.chdir(previous_directory)

    assert config.seed == 20260805
    assert config.output_directory == (repository_root / "output").resolve()
    assert [entity.name for entity in config.entities] == ["provider", "member", "claim"]
    provider, member, claim = config.entities
    assert provider.name == "provider"
    assert provider.count == 10
    assert provider.profile == "provider"
    assert provider.scenarios == {
        "new": 1,
        "changed": 1,
        "duplicate": 1,
        "stale": 1,
        "incomplete": 1,
    }
    assert provider.schema == (repository_root / "schemas/provider/provider.schema.json").resolve()
    assert member.count == 10
    assert member.profile == "member"
    assert member.scenarios == provider.scenarios
    assert member.schema == (repository_root / "schemas/member/member.schema.json").resolve()
    assert claim.count == 20
    assert claim.profile == "claim-professional"
    assert claim.scenarios == {
        **provider.scenarios,
        "replacement": 1,
        "void": 1,
        "orphan_payment": 1,
    }
    assert claim.schema == (repository_root / "schemas/claim/claim.schema.json").resolve()
    assert config.survivorship_policy.member_verified_action == "update"
    assert config.survivorship_policy.claim_verified_action == "update"
    assert config.survivorship_policy.void_action == "ignore"

    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        minimal_config = {
            "member": {
                "count": 10,
                "scenarios": {
                    "new": 1,
                    "changed": 1,
                    "duplicate": 1,
                    "stale": 1,
                    "incomplete": 1,
                },
            }
        }
        config_path = root / "minimal.config.json"
        _write_json(config_path, minimal_config)
        loaded = load_config(config_path)
        assert loaded.seed == 20260805
        assert loaded.output_directory == (root / "output").resolve()
        assert [entity.name for entity in loaded.entities] == ["member"]
        assert loaded.entities[0].scenarios == minimal_config["member"]["scenarios"]
        assert loaded.disabled_filenames == ("providers.jsonl", "claims.jsonl")

        _write_json(config_path, {"claim": {"count": 2, "scenarios": {"new": 1}}})
        claim_only = load_config(config_path)
        assert [entity.name for entity in claim_only.entities] == ["claim"]
        assert claim_only.entities[0].filename == "claims.jsonl"

    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        scenario_config = _load_json(repository_root / "generator.config.json")
        scenario_config["output_directory"] = "./output"
        for entity in scenario_config["entities"].values():
            entity["schema"] = str((repository_root / entity["schema"]).resolve())
        scenario_config["entities"]["member"]["profile"] = "member"
        scenario_config["entities"]["member"]["scenarios"] = {
            "new": 1,
            "changed": 1,
            "duplicate": 1,
            "stale": 1,
            "incomplete": 1,
        }
        scenario_config["survivorship_policy"] = {
            "member_verified_action": "keep_both",
            "claim_verified_action": "update",
            "void_action": "ignore",
        }
        config_path = root / "generator.config.json"
        _write_json(config_path, scenario_config)
        loaded = load_config(config_path)
        loaded_member = next(entity for entity in loaded.entities if entity.name == "member")
        assert loaded_member.scenarios == scenario_config["entities"]["member"]["scenarios"]
        assert loaded.survivorship_policy.member_verified_action == "keep_both"
        assert not (root / "output").exists()

        scenario_config["entities"]["member"]["scenarios"]["new"] = 7
        _write_json(config_path, scenario_config)
        try:
            load_config(config_path)
        except ConfigurationError as error:
            assert "Scenario total" in str(error)
        else:
            raise AssertionError("scenario quantities above count must be rejected")
        assert not (root / "output").exists()

        scenario_config["entities"]["member"]["scenarios"] = {"new": 9, "changed": 1}
        _write_json(config_path, scenario_config)
        try:
            load_config(config_path)
        except ConfigurationError as error:
            assert "require at least one baseline record" in str(error)
        else:
            raise AssertionError("non-new scenarios without a baseline must be rejected")
        assert not (root / "output").exists()

        scenario_config["entities"]["member"]["scenarios"] = {"new": 10}
        _write_json(config_path, scenario_config)
        all_new_config = load_config(config_path)
        all_new_member = next(
            entity for entity in all_new_config.entities if entity.name == "member"
        )
        assert all_new_member.scenarios == {"new": 10}
        assert not (root / "output").exists()

        scenario_config["entities"]["member"]["scenarios"] = {"replacement": 1}
        _write_json(config_path, scenario_config)
        try:
            load_config(config_path)
        except ConfigurationError as error:
            assert "not supported" in str(error)
        else:
            raise AssertionError("unsupported scenarios must be rejected")
        assert not (root / "output").exists()

        scenario_config["entities"]["member"]["scenarios"] = {"new": -1}
        _write_json(config_path, scenario_config)
        try:
            load_config(config_path)
        except ConfigurationError as error:
            assert "at least 0" in str(error)
        else:
            raise AssertionError("negative scenario quantities must be rejected")
        assert not (root / "output").exists()

        scenario_config["entities"]["member"]["scenarios"] = {}
        scenario_config["entities"]["member"]["profile"] = "unknown"
        _write_json(config_path, scenario_config)
        try:
            load_config(config_path)
        except ConfigurationError as error:
            assert "unknown layout profile" in str(error)
        else:
            raise AssertionError("unknown layout profiles must be rejected")
        assert not (root / "output").exists()

    with TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        invalid_config = _load_json(repository_root / "generator.config.json")
        invalid_config["output_directory"] = "./output"
        invalid_config["entities"]["provider"]["schema"] = "./missing.schema.json"
        config_path = root / "generator.config.json"
        _write_json(config_path, invalid_config)
        try:
            load_config(config_path)
        except ConfigurationError:
            pass
        else:
            raise AssertionError("missing enabled schemas must be rejected")
        assert not (root / "output").exists()

    for unsafe_filename in (
        "/tmp/providers.jsonl",
        "./providers.jsonl",
        "nested/../providers.jsonl",
        r"nested\..\providers.jsonl",
        r"C:\outside\providers.jsonl",
    ):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            invalid_config = _load_json(repository_root / "generator.config.json")
            invalid_config["output_directory"] = "./output"
            invalid_config["entities"]["provider"]["schema"] = str(
                repository_root / "schemas/provider/provider.schema.json"
            )
            invalid_config["entities"]["provider"]["filename"] = unsafe_filename
            config_path = root / "generator.config.json"
            _write_json(config_path, invalid_config)
            try:
                load_config(config_path)
            except ConfigurationError as error:
                assert "Invalid filename for entity 'provider'" in str(error)
                assert unsafe_filename not in str(error)
            else:
                raise AssertionError(f"unsafe filename was accepted: {unsafe_filename!r}")
            assert not (root / "output").exists()


def _load_json(path: Path) -> dict[str, Any]:
    """Read one JSON object used as a temporary smoke input.

    Args:
        path: JSON file to read.

    Returns:
        Parsed JSON object.

    Raises:
        AssertionError: If the JSON root is not an object.
    """
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError("smoke fixture must be a JSON object")
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    """Write one temporary JSON object for an invalid-config smoke check.

    Args:
        path: Temporary file to create.
        value: JSON object to serialize.
    """
    path.write_text(json.dumps(value), encoding="utf-8")


if __name__ == "__main__":
    main()
