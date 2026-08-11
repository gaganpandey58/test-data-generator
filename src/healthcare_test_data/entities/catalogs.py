"""Load checked-in, complete GDF field catalogs for supported entities.

Catalogs represent every attribute supplied by the GDF workbook.  Layouts use
their own smaller selection later in the generation pipeline; this module is
the stable source for fields that are available but not yet emitted.
"""

from __future__ import annotations

import json
from importlib.resources import files


def load_entity_catalog(entity: str) -> tuple[str, ...]:
    """Return every ordered GDF field defined for one supported entity.

    Args:
        entity: Stable entity name such as ``provider``, ``member``, or ``claim``.

    Returns:
        The complete de-duplicated field sequence checked into the package.

    Raises:
        FileNotFoundError: If no checked-in catalog exists for ``entity``.
    """
    catalog_file = files(__package__).joinpath("catalogs", f"{entity}.json")
    payload = json.loads(catalog_file.read_text(encoding="utf-8"))
    return tuple(payload["fields"])
