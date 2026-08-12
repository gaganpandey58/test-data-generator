"""Exercise the small public generation contract and source-shaped output."""

from __future__ import annotations

import json
from pathlib import Path

from healthcare_test_data.cli import generate
from healthcare_test_data.config import load_config
from healthcare_test_data.layouts import deduplicate_nested_fields, load_layout
from healthcare_test_data.sample_shapes import available_sources, blank_record


def _config(tmp_path: Path) -> Path:
    """Write a minimal all-stream configuration for one integration test."""
    path = tmp_path / "generator.config.json"
    path.write_text(
        json.dumps(
            {
                "client": "chc",
                "seed": 7,
                "output_directory": str(tmp_path / "output"),
                "provider": {"count": 1},
                "member": {"count": 1},
                "claims": {"professional": {"count": 1}, "institutional": {"count": 1}},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_simple_config_resolves_four_separate_streams(tmp_path: Path) -> None:
    """Keep entity choice, client, and count as the only public controls."""
    config = load_config(_config(tmp_path))
    assert [entity.filename for entity in config.entities] == [
        "providers.jsonl",
        "members.jsonl",
        "professional-claims.jsonl",
        "institutional-claims.jsonl",
    ]


def test_public_selection_can_name_its_compatible_layout(tmp_path: Path) -> None:
    """Keep optional layout selection small and restricted to the data type."""
    path = _config(tmp_path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["provider"]["layout"] = "provider"
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert load_config(path).entities[0].profile == "provider"


def test_claim_stream_can_run_without_member_or_provider_output(tmp_path: Path) -> None:
    """Allow a selected claim stream to create deterministic linked IDs alone."""
    path = tmp_path / "claims-only.json"
    path.write_text(
        json.dumps(
            {
                "client": "chc",
                "seed": 7,
                "output_directory": str(tmp_path / "output"),
                "claims": {"professional": {"count": 2}},
            }
        ),
        encoding="utf-8",
    )
    generate(path)
    rows = [
        json.loads(line)
        for line in (tmp_path / "output" / "professional-claims.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 2
    assert rows[0]["CH_PATIENT_CLIENT_ID"].startswith("MBR")
    assert rows[0]["CH_BILLING_PROVIDER_CLIENT_ID"].startswith("PPRV")


def test_packaged_sample_shapes_cover_every_supplied_data_source() -> None:
    """Keep provider, member, claim, and payment defaults in one small file."""
    assert available_sources() == {
        "provider",
        "member",
        "claim_professional",
        "claim_institutional",
        "payment_professional",
        "payment_institutional",
    }
    assert "CP_PROVIDER_CLIENT_ID" in blank_record("provider")
    assert "CM_MEMBER_CLIENT_ID" in blank_record("member")
    assert "CLAIM_DETAIL" in blank_record("claim_professional")
    assert "CLAIM_DETAIL" in blank_record("payment_institutional")


def test_generation_uses_client_headers_and_sample_layouts(tmp_path: Path) -> None:
    """Generate every stream with its declared headers, groups, and claim type."""
    config_path = _config(tmp_path)
    generate(config_path)
    output = tmp_path / "output"
    provider = json.loads((output / "providers.jsonl").read_text().splitlines()[0])
    member = json.loads((output / "members.jsonl").read_text().splitlines()[0])
    professional = json.loads((output / "professional-claims.jsonl").read_text().splitlines()[0])
    institutional = json.loads((output / "institutional-claims.jsonl").read_text().splitlines()[0])
    assert provider["PAYER"] == member["PAYER"] == professional["PAYER"] == "CHC"
    assert member["CM_MEMBER_COB"]
    assert professional["CH_CLAIM_TYPE"] == "P"
    assert institutional["CH_CLAIM_TYPE"] == "O"
    assert set(member["CM_MEMBER_COB"][0]) == {
        field.name for field in load_layout("member").groups["CM_MEMBER_COB"]
    }


def test_claim_roots_keep_their_own_sample_json_kinds(tmp_path: Path) -> None:
    """Use each claim sample as the authority when payment fields overlap.

    The payment samples contribute payment-only fields, but they must not
    change a shared claim-header field's JSON type.  The institutional source
    deliberately represents ``CH_PLACE_OF_SERVICE_CODE`` as an integer.
    """
    generate(_config(tmp_path))
    output = tmp_path / "output"
    patterns = json.loads(
        (Path(__file__).parents[1] / "src/healthcare_test_data/sample_shapes.json").read_text()
    )["sources"]
    for filename, source in (
        ("professional-claims.jsonl", "claim_professional"),
        ("institutional-claims.jsonl", "claim_institutional"),
    ):
        row = json.loads((output / filename).read_text().splitlines()[0])
        expected = patterns[source]
        for name, kind in expected.items():
            if isinstance(kind, str):
                assert type(row[name]) is _json_kind_type(kind), name
    institutional = json.loads((output / "institutional-claims.jsonl").read_text().splitlines()[0])
    assert type(institutional["CH_PLACE_OF_SERVICE_CODE"]) is int


def _json_kind_type(kind: str) -> type[object]:
    """Map a packaged sample JSON-kind marker to its exact Python type."""
    return {"s": str, "i": int, "n": float, "b": bool, "z": type(None)}[kind]


def test_nested_deduplication_obeys_layout_retention_rules() -> None:
    """Retain only nested identifiers that layouts explicitly require."""
    parent = {"id": "parent", "shared": "same"}
    nested = {"id": "parent", "shared": "same", "child": "value"}
    assert deduplicate_nested_fields(nested, parent, frozenset()) == {"child": "value"}
    assert deduplicate_nested_fields(nested, parent, frozenset({"id"})) == {
        "id": "parent",
        "child": "value",
    }


def test_layouts_select_only_sample_fields_plus_requested_member_cob() -> None:
    """Keep GDF-only available fields out of the current JSON output contract."""
    source_sets = {
        "provider": ("provider",),
        "member": ("member",),
        "claim-professional": ("claim_professional", "payment_professional"),
        "claim-institutional": ("claim_institutional", "payment_institutional"),
    }
    patterns = json.loads(
        (Path(__file__).parents[1] / "src/healthcare_test_data/sample_shapes.json").read_text()
    )["sources"]
    for profile, sources in source_sets.items():
        expected_root: set[str] = set()
        expected_groups: dict[str, set[str]] = {}
        for source in sources:
            for name, value in patterns[source].items():
                if isinstance(value, list):
                    expected_groups.setdefault(name, set())
                    if value and isinstance(value[0], dict):
                        expected_groups[name].update(value[0])
                else:
                    expected_root.add(name)
        layout = load_layout(profile)
        actual_root = {field.name for field in (*layout.headers, *layout.root)}
        assert actual_root == expected_root
        assert set(layout.groups) == set(expected_groups) | (
            {"CM_MEMBER_COB"} if profile == "member" else set()
        )
        for group, expected_fields in expected_groups.items():
            assert {field.name for field in layout.groups[group]} == expected_fields
