"""Regression coverage for the source-controlled GDF field catalogs."""

from __future__ import annotations

import json
import os
import runpy
from pathlib import Path

import pytest

from healthcare_test_data.entities.catalogs import load_entity_catalog

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_FIELD_COUNTS = {"provider": 103, "member": 338, "claim": 822}
DEFAULT_GDF_WORKBOOK = Path(
    "/Users/gpandey/Downloads/GDF Request File Layouts Standard - v2.9 - Copy(3).xlsx"
)


@pytest.mark.parametrize("entity", ("provider", "member", "claim"))
def test_schema_covers_every_checked_in_gdf_catalog_field(entity: str) -> None:
    """Ensure every field available in the GDF catalog has a schema declaration."""
    schema_path = PROJECT_ROOT / "schemas" / entity / f"{entity}.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    missing = set(load_entity_catalog(entity)).difference(schema["properties"])
    assert not missing, f"{entity} schema omits catalog fields: {sorted(missing)}"


@pytest.mark.parametrize("entity", ("provider", "member", "claim"))
def test_entity_catalog_is_ordered_and_deduplicated(entity: str) -> None:
    """Ensure each entity's checked-in catalog remains a stable field inventory."""
    fields = load_entity_catalog(entity)
    assert fields
    assert len(fields) == len(set(fields))


@pytest.mark.parametrize("entity", ("provider", "member", "claim"))
def test_entity_catalog_matches_the_expected_gdf_field_count(entity: str) -> None:
    """Protect complete GDF support even when the source workbook is unavailable."""
    assert len(load_entity_catalog(entity)) == EXPECTED_FIELD_COUNTS[entity]


def test_catalogs_match_the_supplied_gdf_workbook_when_available() -> None:
    """Compare checked-in catalogs to the supplied GDF workbook when it is present.

    CI can omit the private source workbook and still use the fixed expected
    counts above.  Maintainers with the supplied workbook receive the stronger
    direct regression check before refreshing the checked-in catalogs.
    """
    workbook = Path(os.environ.get("GDF_WORKBOOK", DEFAULT_GDF_WORKBOOK))
    if not workbook.is_file():
        pytest.skip("The supplied GDF workbook is not available in this environment.")
    module = runpy.run_path(str(PROJECT_ROOT / "scripts" / "extract_gdf_catalogs.py"))
    extracted = module["extract_catalogs"](workbook)
    assert extracted == {entity: list(load_entity_catalog(entity)) for entity in extracted}
