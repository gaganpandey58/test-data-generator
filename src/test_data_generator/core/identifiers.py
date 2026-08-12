"""Create deterministic identifiers that preserve source-system wire formats."""

from hashlib import sha256
from uuid import UUID


def deterministic_uuid4(seed: int, namespace: str) -> str:
    """Return a stable UUIDv4-format identifier for a generation namespace.

    Source samples use UUID version 4 for ``ROWID`` values. A random UUID4
    would make seeded generation non-repeatable, so this helper derives 16
    bytes from the seed and namespace, then applies the UUIDv4 version and RFC
    4122 variant bits before formatting the value.

    Args:
        seed: Shared deterministic seed from the run configuration.
        namespace: Stable entity, purpose, and row identifier.

    Returns:
        A lowercase UUID string with UUID version 4 and RFC 4122 variant bits.
    """
    value = bytearray(sha256(f"{seed}:{namespace}".encode("utf-8")).digest()[:16])
    value[6] = (value[6] & 0x0F) | 0x40
    value[8] = (value[8] & 0x3F) | 0x80
    return str(UUID(bytes=bytes(value)))
