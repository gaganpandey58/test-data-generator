"""Extract source-controlled entity field catalogs from the GDF workbook.

The generator never reads the workbook at runtime.  This utility is only used
when a new GDF layout version needs to refresh the checked-in catalog JSON
files that document every field available to each supported entity.
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


def write_catalogs(workbook_path: Path, output_directory: Path) -> None:
    """Write stable, source-controlled JSON catalogs from one GDF workbook."""
    catalogs = extract_catalogs(workbook_path)
    output_directory.mkdir(parents=True, exist_ok=True)
    for entity, fields in catalogs.items():
        payload = {
            "source": workbook_path.name,
            "entity": entity,
            "fields": fields,
        }
        (output_directory / f"{entity}.json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )


def main(arguments: list[str]) -> int:
    """Refresh catalogs using a workbook path and optional output directory."""
    if not arguments:
        raise SystemExit("Usage: extract_gdf_catalogs.py WORKBOOK [OUTPUT_DIRECTORY]")
    workbook_path = Path(arguments[0]).expanduser().resolve()
    output_directory = (
        Path(arguments[1]).resolve()
        if len(arguments) > 1
        else Path("src/healthcare_test_data/entities/catalogs").resolve()
    )
    write_catalogs(workbook_path, output_directory)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
