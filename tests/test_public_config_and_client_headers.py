"""Cover the small public configuration and client header profile boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from healthcare_test_data import config as config_module
from healthcare_test_data.config import load_config
from healthcare_test_data.engine import run_entity
from healthcare_test_data.entities import claim, member, provider
from healthcare_test_data.errors import ConfigurationError
from healthcare_test_data.layouts import deduplicate_nested_fields, load_layout, project_record


def _write_config(tmp_path: Path, config: dict[str, object]) -> Path:
    """Write one public configuration fixture and return its path.

    Args:
        tmp_path: Pytest-owned temporary directory.
        config: JSON-compatible configuration content.

    Returns:
        Path to the written configuration file.
    """
    path = tmp_path / "generator.config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def test_simple_happy_path_config_resolves_internal_defaults(tmp_path: Path) -> None:
    """Accept client, one happy-path selector, and entity counts only."""
    config = load_config(
        _write_config(
            tmp_path,
            {
                "client": "chc",
                "scenario": "happy-path",
                "seed": 7,
                "output_directory": "generated",
                "provider": {"count": 2},
                "member": {"count": 3},
                "claims": {"professional": {"count": 4}, "institutional": {"count": 5}},
            },
        )
    )

    assert config.client == "chc"
    assert config.scenario == "happy-path"
    assert [entity.name for entity in config.entities] == [
        "provider",
        "member",
        "claim_professional",
        "claim_institutional",
    ]
    assert [entity.filename for entity in config.entities] == [
        "providers.jsonl",
        "members.jsonl",
        "professional-claims.jsonl",
        "institutional-claims.jsonl",
    ]
    assert all(entity.client_headers["PAYER"] == "CHC" for entity in config.entities)


def test_legacy_scenario_quantities_are_rejected(tmp_path: Path) -> None:
    """Reject the retired per-entity variation map from the public API."""
    path = _write_config(
        tmp_path,
        {
            "client": "chc",
            "scenario": "happy-path",
            "provider": {"count": 1, "scenarios": {"changed": 1}},
        },
    )

    with pytest.raises(ConfigurationError, match="unsupported property"):
        load_config(path)


def test_unknown_client_profile_is_rejected(tmp_path: Path) -> None:
    """Reject client selectors without a checked-in header profile."""
    path = _write_config(
        tmp_path,
        {"client": "unknown", "scenario": "happy-path", "provider": {"count": 1}},
    )

    with pytest.raises(ConfigurationError, match="unknown client"):
        load_config(path)


@pytest.mark.parametrize(
    ("generator", "kwargs"),
    [
        (provider.generate_record, {}),
        (member.generate_record, {}),
        (
            claim.generate_record,
            {
                "entity_counts": {"provider": 1, "member": 1},
                "profile": "claim-professional",
                "entity_name": "claim_professional",
            },
        ),
    ],
)
def test_entity_headers_are_supplied_by_the_client_profile(
    generator: object, kwargs: dict[str, object]
) -> None:
    """Ensure every entity accepts data-driven client header values."""
    assert callable(generator)
    record = generator(7, 0, client_headers={"PAYER": "CLIENT-OVERRIDE"}, **kwargs)
    assert record["PAYER"] == "CLIENT-OVERRIDE"


def test_member_layout_explicitly_selects_the_cob_group() -> None:
    """Keep the source member COB group declared for future output projection."""
    layout = load_layout("member")
    assert "CM_MEMBER_COB" in layout.groups


def test_member_risk_score_preserves_the_numeric_source_sample_kind() -> None:
    """Keep the decimal risk score JSON type used by the member sample."""
    record = member.generate_record(7, 0, client_headers={})
    assert isinstance(record["CM_RISK_SCORE"], float)


def test_member_cob_is_nonempty_and_matches_the_declared_layout() -> None:
    """Generate a sample-compatible COB item rather than an empty placeholder."""
    record = member.generate_record(7, 0, client_headers={})
    cob = record["CM_MEMBER_COB"]
    assert isinstance(cob, list) and len(cob) == 1
    assert set(cob[0]) == {field.name for field in load_layout("member").groups["CM_MEMBER_COB"]}


def test_selected_client_profile_controls_all_platform_and_header_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prove client selection drives root, nested, and claim envelope values."""
    profiles: dict[str, dict[str, object]] = {
        "provider": {
            "PAYER": "VARIANT",
            "PAYER_PLATFORM": "VARIANT-PLATFORM",
            "CLIENT_DATA_PLATFORM": "VARIANT-PLATFORM",
            "CP_CLIENT_DATA_PLATFORM": "VARIANT-PLATFORM",
        },
        "member": {
            "PAYER": "VARIANT",
            "PAYER_PLATFORM": "VARIANT-PLATFORM",
            "CLIENT_DATA_PLATFORM": "VARIANT-PLATFORM",
            "CM_CLIENT_DATA_PLATFORM": "VARIANT-PLATFORM",
            "CM_PAYER_SHORT": "VARIANT",
        },
        "claim_professional": {
            "PAYER": "VARIANT",
            "PAYER_PLATFORM": "VARIANT-PLATFORM",
            "CLIENT_DATA_PLATFORM": "VARIANT-PLATFORM",
            "CH_CLIENT_DATA_PLATFORM": "VARIANT-PLATFORM",
            "otherAttributes.payerName": "VARIANT PAYER",
            "otherAttributes.payerIdentifier": "VARIANT-ID",
        },
        "claim_institutional": {
            "PAYER": "VARIANT",
            "PAYER_PLATFORM": "VARIANT-PLATFORM",
            "CLIENT_DATA_PLATFORM": "VARIANT-PLATFORM",
            "CH_CLIENT_DATA_PLATFORM": "VARIANT-PLATFORM",
        },
    }

    monkeypatch.setattr(config_module, "available_clients", lambda: frozenset({"variant"}))
    monkeypatch.setattr(
        config_module,
        "load_client_headers",
        lambda client, entity: profiles[entity],
    )
    monkeypatch.setattr(config_module, "load_client_values", lambda client, entity: {})
    config = load_config(
        _write_config(
            tmp_path,
            {
                "client": "variant",
                "scenario": "happy-path",
                "provider": {"count": 1},
                "member": {"count": 1},
                "claims": {"professional": {"count": 1}},
            },
        )
    )
    selected = {entity.name: entity.client_headers for entity in config.entities}

    provider_record = provider.generate_record(7, 0, client_headers=selected["provider"])
    member_record = member.generate_record(7, 0, client_headers=selected["member"])
    claim_record = claim.generate_record(
        7,
        0,
        {"provider": 1, "member": 1},
        profile="claim-professional",
        entity_name="claim_professional",
        client_headers=selected["claim_professional"],
    )

    assert provider_record["CP_CLIENT_DATA_PLATFORM"] == "VARIANT-PLATFORM"
    assert (
        provider_record["CP_PROVIDER_ADDRESSES"][0]["CP_CLIENT_DATA_PLATFORM"]
        == "VARIANT-PLATFORM"
    )
    assert (
        provider_record["CP_PROVIDER_NETWORKS"][0]["CP_CLIENT_DATA_PLATFORM"]
        == "VARIANT-PLATFORM"
    )
    assert member_record["CM_CLIENT_DATA_PLATFORM"] == "VARIANT-PLATFORM"
    assert member_record["CM_PAYER_SHORT"] == "VARIANT"
    assert member_record["CM_MEMBER_ADDRESSES"][0]["CM_CLIENT_DATA_PLATFORM"] == "VARIANT-PLATFORM"
    assert claim_record["CH_CLIENT_DATA_PLATFORM"] == "VARIANT-PLATFORM"
    assert claim_record["otherAttributes"]["payerName"] == "VARIANT PAYER"
    assert "otherAttributes.payerName" not in claim_record
    assert all("." not in key for key in claim_record if key.startswith("otherAttributes"))


def test_layouts_acknowledge_common_transport_headers() -> None:
    """Declare envelope fields in every layout without treating them as GDF body fields."""
    expected = {"PAYER", "PAYER_PLATFORM", "CLIENT_DATA_PLATFORM", "ROWID"}
    for profile in ("provider", "member", "claim-professional", "claim-institutional"):
        assert expected <= {field.name for field in load_layout(profile).headers}


def test_layouts_declare_every_emitted_transport_header() -> None:
    """Keep layout header metadata complete for each generated EIP envelope."""
    common = {
        "FILE_TYPE",
        "INGESTION_DATE",
        "INGESTION_EPOCH",
        "PAYER",
        "PAYER_PLATFORM",
        "CLIENT_DATA_PLATFORM",
        "PUBLISHER_NAME",
        "PRODUCT",
        "GDF_VERSION",
        "DATA_CATEGORY",
        "LOB",
        "cotiviti.dataset_id",
        "cotiviti.tenant_id",
        "cotiviti.schema_version",
        "cotiviti.client_id",
        "cotiviti.client_system",
        "cotiviti.message_id",
        "cotiviti.produced_at",
        "cotiviti.source_format",
        "cotiviti.source_system",
        "cotiviti.batch_id",
        "cotiviti.message_seq",
        "cotiviti.correlation_id",
        "cotiviti.source.raw_file_ref",
    }
    expected = {
        "provider": common | {"ROWID"},
        "member": common
        | {
            "ROWID",
            "cotiviti.producer_version",
            "cotiviti.source.isa_control",
            "cotiviti.source.gs_control",
        },
        "claim-professional": common
        | {
            "otherAttributes",
            "cotiviti.producer_version",
            "cotiviti.source.isa_control",
            "cotiviti.source.gs_control",
            "cotiviti.source.st_control",
            "cotiviti.source.claim_id",
            "x-connector-name",
        },
        "claim-institutional": common
        | {
            "ROWID",
            "otherAttributes",
            "cotiviti.producer_version",
            "cotiviti.source.isa_control",
            "cotiviti.source.gs_control",
            "cotiviti.source.st_control",
            "cotiviti.source.claim_id",
            "x-connector-name",
        },
    }
    for profile, fields in expected.items():
        assert fields <= {field.name for field in load_layout(profile).headers}


def test_complete_alternate_profile_flows_from_config_through_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use a full client profile to alter emitted data in generated JSONL files."""
    headers = {
        "PAYER": "ALTERNATE",
        "PAYER_PLATFORM": "ALT-PLATFORM",
        "CLIENT_DATA_PLATFORM": "ALT-PLATFORM",
        "CP_CLIENT_DATA_PLATFORM": "ALT-PLATFORM",
        "CM_CLIENT_DATA_PLATFORM": "ALT-PLATFORM",
        "CM_PAYER_SHORT": "ALTERNATE",
        "CH_CLIENT_DATA_PLATFORM": "ALT-CLAIMS",
        "LOB": "alt-lob",
        "cotiviti.tenant_id": "alt-tenant",
        "cotiviti.client_id": "alt-client",
        "cotiviti.client_system": "alt-system",
        "otherAttributes.payerName": "ALTERNATE",
        "otherAttributes.payerIdentifier": "ALT",
        "unexpected.path": "must-not-leak",
    }
    values = {
        "provider_network_id_prefix": "ALT-NET",
        "provider_network_name": "Alternate Network",
        "member_network_client_id": "ALT-NET",
    }
    monkeypatch.setattr(config_module, "available_clients", lambda: frozenset({"alternate"}))
    monkeypatch.setattr(
        config_module,
        "load_client_headers",
        lambda client, entity: headers | {"PRODUCT": "PPC", "cotiviti.source_system": "PPC"},
    )
    monkeypatch.setattr(config_module, "load_client_values", lambda client, entity: values)
    config = load_config(
        _write_config(
            tmp_path,
            {
                "client": "alternate",
                "scenario": "happy-path",
                "output_directory": str(tmp_path / "output"),
                "provider": {"count": 1},
                "member": {"count": 1},
                "claims": {"professional": {"count": 1}},
            },
        )
    )
    counts = {entity.name: entity.count for entity in config.entities}
    paths = {
        entity.name: run_entity(entity, config.seed, config.output_directory, counts)
        for entity in config.entities
    }
    provider_record = json.loads(paths["provider"].read_text().splitlines()[0])
    member_record = json.loads(paths["member"].read_text().splitlines()[0])
    claim_record = json.loads(paths["claim_professional"].read_text().splitlines()[0])

    assert provider_record["PAYER"] == "ALTERNATE"
    network = provider_record["CP_PROVIDER_NETWORKS"][0]
    assert network["CP_PROVIDER_NETWORK_CLIENT_ID"].startswith("ALT-NET-")
    assert network["CP_PROVIDER_NETWORK_NAME"] == "Alternate Network"
    assert member_record["CM_MEMBER_ENROLLMENTS"][0]["CM_NETWORK_CLIENT_ID"] == "ALT-NET"
    assert member_record["LOB"] == "alt-lob"
    assert claim_record["cotiviti.tenant_id"] == "alt-tenant"
    assert "unexpected.path" not in claim_record


def test_layout_projection_removes_undeclared_root_and_nested_fields() -> None:
    """Enforce the layout as the sole output-selection contract."""
    projected = project_record(
        {
            "CP_PROVIDER_CLIENT_ID": "provider-1",
            "PAYER": "client",
            "not_declared": "must-not-emit",
            "CP_PROVIDER_ADDRESSES": [
                {
                    "CP_PROVIDER_ADDRESS_01": "1 Main Street",
                    "not_declared": "must-not-emit",
                }
            ],
        },
        "provider",
    )
    assert "not_declared" not in projected
    assert "not_declared" not in projected["CP_PROVIDER_ADDRESSES"][0]


def test_nested_deduplication_is_generic_with_declarative_retention() -> None:
    """Remove redundant parent fields unless layout metadata requires a reference."""
    parent = {"id": "parent", "shared": "same"}
    nested = {"id": "parent", "shared": "same", "child": "value"}
    assert deduplicate_nested_fields(nested, parent, frozenset()) == {"child": "value"}
    assert deduplicate_nested_fields(nested, parent, frozenset({"id"})) == {
        "id": "parent",
        "child": "value",
    }


def test_provider_layout_retains_declared_relationship_references() -> None:
    """Preserve source-required provider links while deduplicating other nested values."""
    projected = project_record(
        {
            "CP_PROVIDER_CLIENT_ID": "provider-1",
            "CP_PROVIDER_CLIENT_MASTER_ID": "master-1",
            "CP_PROVIDER_ADDRESSES": [
                {
                    "CP_PROVIDER_CLIENT_ID": "provider-1",
                    "CP_PROVIDER_CLIENT_MASTER_ID": "master-1",
                    "CP_PROVIDER_ADDRESS_01": "1 Main Street",
                }
            ],
        },
        "provider",
    )
    address = projected["CP_PROVIDER_ADDRESSES"][0]
    assert address["CP_PROVIDER_CLIENT_ID"] == "provider-1"
    assert address["CP_PROVIDER_CLIENT_MASTER_ID"] == "master-1"
