"""Regression coverage for GDF field availability in the JSON Schemas."""

from __future__ import annotations

import json
import os
import runpy
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_FIELD_COUNTS = {"provider": 103, "member": 338, "claim": 822}
DEFAULT_GDF_WORKBOOK = Path(
    "/Users/gpandey/Downloads/GDF Request File Layouts Standard - v2.9 - Copy(3).xlsx"
)


def _schema_fields(entity: str) -> set[str]:
    """Return every property declared by one complete entity schema."""
    schema_path = PROJECT_ROOT / "schemas" / entity / f"{entity}.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return set(schema["properties"])


def test_extractor_adds_only_missing_schema_properties() -> None:
    """Keep the GDF refresh tool focused on schemas, not a second catalog.

    New fields are optional available attributes because layouts, rather than
    the extractor, decide what current JSONL output contains.
    """
    module = runpy.run_path(str(PROJECT_ROOT / "scripts" / "extract_gdf_catalogs.py"))
    schema: dict[str, object] = {"properties": {"KNOWN": {"type": "string"}}}
    added = module["update_schema_properties"](schema, ["KNOWN", "NEW_FIELD"])
    assert added == ["NEW_FIELD"]
    assert schema == {
        "properties": {
            "KNOWN": {"type": "string"},
            "NEW_FIELD": {},
        }
    }


def test_schemas_match_the_supplied_gdf_workbook_when_available() -> None:
    """Ensure the supplied GDF fields remain available in their schemas.

    CI can omit the private source workbook and still use the fixed expected
    counts above.  Maintainers with the supplied workbook receive the stronger
    direct regression check before refreshing the complete schemas.
    """
    workbook = Path(os.environ.get("GDF_WORKBOOK", DEFAULT_GDF_WORKBOOK))
    if not workbook.is_file():
        pytest.skip("The supplied GDF workbook is not available in this environment.")
    module = runpy.run_path(str(PROJECT_ROOT / "scripts" / "extract_gdf_catalogs.py"))
    extracted = module["extract_catalogs"](workbook)
    for entity, fields in extracted.items():
        assert len(fields) == EXPECTED_FIELD_COUNTS[entity]
        missing = set(fields).difference(_schema_fields(entity))
        assert not missing, f"{entity} schema omits GDF fields: {sorted(missing)}"
