"""Extract auditable table text and revision metadata from the survivorship DOCX.

This tool intentionally preserves source wording. It is an audit aid for
reviewing the checked-in normalized catalog; it does not invent weights or
classifications that are absent from the source document.
"""

import argparse
import json
import re
from pathlib import Path
from zipfile import ZipFile
from xml.etree import ElementTree


WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def extract_source(path: Path) -> dict[str, object]:
    """Extract paragraphs, tables, and revision rows from one DOCX."""
    with ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    body = root.find(f"{WORD_NS}body")
    if body is None:
        raise ValueError("DOCX has no document body")
    paragraphs: list[str] = []
    tables: list[list[list[str]]] = []
    for child in body:
        if child.tag == f"{WORD_NS}p":
            text = _text(child)
            if text:
                paragraphs.append(text)
        elif child.tag == f"{WORD_NS}tbl":
            rows: list[list[str]] = []
            for row in child.findall(f"{WORD_NS}tr"):
                rows.append([_text(cell) for cell in row.findall(f"{WORD_NS}tc")])
            tables.append(rows)
    revision_rows = tables[0] if tables else []
    revision = next((row for row in revision_rows if row and row[0] == "0.9"), [])
    return {
        "source_document": path.name,
        "paragraphs": paragraphs,
        "tables": tables,
        "latest_revision": {
            "version": revision[0] if revision else "",
            "date": revision[1] if len(revision) > 1 else "",
            "description": revision[3] if len(revision) > 3 else "",
            "status": revision[4] if len(revision) > 4 else "",
        },
    }


def validate_catalog_coverage(source: dict[str, object], catalog_path: Path) -> list[str]:
    """Return GDF fields named by the DOCX but absent from catalog source_fields."""
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog_fields = {
        field
        for entity in catalog.get("entities", {}).values()
        for field in entity.get("source_fields", [])
    }
    document_fields = {
        field
        for table in source.get("tables", [])
        for row in table
        for cell in row
        for field in re.findall(
            r"\b(?:CM|CP|CH|CD)_[A-Z0-9_]+",
            re.sub(r"(?=(?:CM|CP|CH|CD)_)", " ", cell),
        )
    }
    return sorted(document_fields - catalog_fields)


def _text(node: ElementTree.Element) -> str:
    return re.sub(r"\s+", " ", "".join(node.itertext())).strip()


def main() -> int:
    """Run the DOCX source extraction command."""
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--check-catalog", action="store_true")
    args = parser.parse_args()
    source = extract_source(args.source)
    missing = validate_catalog_coverage(source, args.catalog) if args.catalog else []
    if args.check_catalog and missing:
        raise SystemExit("Catalog is missing DOCX fields: " + ", ".join(missing))
    source["catalog_coverage"] = {"missing_fields": missing, "complete": not missing}
    args.output.write_text(
        json.dumps(source, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
