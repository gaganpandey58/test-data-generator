"""Refresh or verify GDF field properties in the checked-in JSON Schemas.

The GDF workbook is the source of every available provider, member, and claim
attribute.  The generator does not read the workbook at runtime: this small
maintenance utility adds newly available field names to the schemas, or checks
that the schemas already contain them.
"""

from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as element_tree
import zipfile
from collections.abc import Iterable
from pathlib import Path

WORKSHEET_NAMESPACE = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELATIONSHIP_NAMESPACE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
SPREADSHEET_NAMESPACE = {"main": WORKSHEET_NAMESPACE}
CELL_REFERENCE_PATTERN = re.compile(r"([A-Z]+)")

ENTITY_SHEETS = {
    "claim": ("Medical Claims",),
    "member": (
        "Member",
        "Member Address",
        "Member COB",
        "Member Enrollment",
        "Member MMR",
        "Member Delivery",
    ),
    "provider": (
        "Provider",
        "Provider Network",
        "Provider Address",
        "Provider Client Metrics",
        "Provider Client Group",
        "Provider Specialty",
    ),
}


def _column_number(reference: str) -> int:
    """Return the zero-based column number represented by an Excel reference."""
    letters = CELL_REFERENCE_PATTERN.match(reference)
    if letters is None:
        raise ValueError(f"Cell reference {reference!r} does not begin with a column.")
    value = 0
    for letter in letters.group(1):
        value = value * 26 + ord(letter) - ord("A") + 1
    return value - 1


def _shared_strings(workbook: zipfile.ZipFile) -> list[str]:
    """Load the workbook shared-string table, returning an empty table when absent."""
    try:
        root = element_tree.fromstring(workbook.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return ["".join(item.itertext()) for item in root.findall("main:si", SPREADSHEET_NAMESPACE)]


def _sheet_paths(workbook: zipfile.ZipFile) -> dict[str, str]:
    """Map worksheet display names to XML paths inside an XLSX archive."""
    workbook_root = element_tree.fromstring(workbook.read("xl/workbook.xml"))
    relationships_root = element_tree.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
    relationships = {
        relationship.attrib["Id"]: relationship.attrib["Target"]
        for relationship in relationships_root
    }
    paths: dict[str, str] = {}
    for sheet in workbook_root.findall("main:sheets/main:sheet", SPREADSHEET_NAMESPACE):
        relationship_id = sheet.attrib[f"{{{RELATIONSHIP_NAMESPACE}}}id"]
        target = relationships[relationship_id].lstrip("/")
        paths[sheet.attrib["name"]] = f"xl/{target}" if not target.startswith("xl/") else target
    return paths


def _cell_value(cell: element_tree.Element, shared_strings: list[str]) -> str:
    """Return the displayed scalar value of an XLSX cell."""
    value = cell.find("main:v", SPREADSHEET_NAMESPACE)
    if value is None or value.text is None:
        return ""
    if cell.attrib.get("t") == "s":
        return shared_strings[int(value.text)]
    return value.text


def _rows(workbook: zipfile.ZipFile, path: str, shared_strings: list[str]) -> Iterable[list[str]]:
    """Yield sparse worksheet rows as normalized, positional string lists."""
    root = element_tree.fromstring(workbook.read(path))
    for row in root.findall("main:sheetData/main:row", SPREADSHEET_NAMESPACE):
        values: list[str] = []
        for cell in row.findall("main:c", SPREADSHEET_NAMESPACE):
            index = _column_number(cell.attrib["r"])
            values.extend("" for _ in range(index - len(values)))
            values.append(_cell_value(cell, shared_strings).strip())
        yield values


def _field_name(row: list[str]) -> str | None:
    """Extract a GDF attribute name from the standard worksheet field column."""
    for value in row:
        candidate = value.strip()
        if re.fullmatch(r"(?:CC|CD|CH|CM|CME|CMD|CP|HDR)_[A-Z0-9_]+", candidate):
            return candidate
    return None


def extract_catalogs(workbook_path: Path) -> dict[str, list[str]]:
    """Return the ordered, de-duplicated GDF fields for every supported entity."""
    with zipfile.ZipFile(workbook_path) as workbook:
        strings = _shared_strings(workbook)
        paths = _sheet_paths(workbook)
        catalogs: dict[str, list[str]] = {}
        for entity, sheet_names in ENTITY_SHEETS.items():
            fields: list[str] = []
            for sheet_name in sheet_names:
                for row in _rows(workbook, paths[sheet_name], strings):
                    name = _field_name(row)
                    if name is not None and name not in fields:
                        fields.append(name)
            catalogs[entity] = fields
    return catalogs


SCHEMA_PATHS = {
    "provider": Path("schemas/provider/provider.schema.json"),
    "member": Path("schemas/member/member.schema.json"),
    "claim": Path("schemas/claim/claim.schema.json"),
}


def missing_schema_fields(schema: dict[str, object], fields: Iterable[str]) -> list[str]:
    """Return GDF field names not yet declared in one schema's properties.

    Args:
        schema: Decoded entity schema.
        fields: GDF field names extracted from the workbook.

    Returns:
        Ordered field names that the schema does not currently acknowledge.
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("Schema must contain an object-valued properties mapping.")
    return [field for field in fields if field not in properties]


def update_schema_properties(schema: dict[str, object], fields: Iterable[str]) -> list[str]:
    """Add missing GDF fields as permissive available attributes.

    Existing property definitions are never replaced: schemas may retain a
    tighter type, format, or length constraint for fields emitted today.  New
    GDF fields are intentionally optional until a layout selects them.

    Args:
        schema: Decoded entity schema to update in place.
        fields: GDF field names extracted from the workbook.

    Returns:
        Ordered field names added to the schema.
    """
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("Schema must contain an object-valued properties mapping.")
    missing = missing_schema_fields(schema, fields)
    properties.update({field: {} for field in missing})
    return missing


def refresh_schemas(workbook_path: Path, schema_root: Path, verify: bool = False) -> int:
    """Update or verify all supported schemas against one GDF workbook.

    Args:
        workbook_path: Source GDF workbook.
        schema_root: Project root containing the ``schemas`` directory.
        verify: When true, make no edits and return one if a field is missing.

    Returns:
        Zero when every schema is current; one when verify mode finds missing
        GDF fields.
    """
    catalogs = extract_catalogs(workbook_path)
    stale = False
    for entity, fields in catalogs.items():
        schema_path = schema_root / SCHEMA_PATHS[entity]
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        if not isinstance(schema, dict):
            raise ValueError(f"Schema {schema_path} must contain a JSON object.")
        missing = missing_schema_fields(schema, fields)
        if verify:
            if missing:
                stale = True
                print(f"{entity}: missing {len(missing)} GDF fields")
            else:
                print(f"{entity}: current ({len(fields)} GDF fields)")
            continue
        if missing:
            update_schema_properties(schema, fields)
            schema_path.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
        print(f"{entity}: {'added ' + str(len(missing)) if missing else 'current'}")
    return 1 if stale else 0


def main(arguments: list[str]) -> int:
    """Refresh schemas from a workbook, or verify them with ``--verify``.

    Usage:
        ``extract_gdf_catalogs.py WORKBOOK [--verify]``
    """
    if not arguments or len(arguments) > 2 or (len(arguments) == 2 and arguments[1] != "--verify"):
        raise SystemExit("Usage: extract_gdf_catalogs.py WORKBOOK [--verify]")
    workbook_path = Path(arguments[0]).expanduser().resolve()
    return refresh_schemas(
        workbook_path, Path(__file__).resolve().parents[1], verify="--verify" in arguments
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
