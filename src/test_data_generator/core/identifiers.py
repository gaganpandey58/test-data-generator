"""Create deterministic identifiers that preserve source-system wire formats."""

from hashlib import sha256
from random import Random
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


def valid_npi(randomizer: Random) -> str:
    """Generate a ten-digit National Provider Identifier with a valid Luhn check digit.

    NPI validation applies the Luhn checksum to the ``80840`` prefix followed
    by the ten digit identifier.  Keeping the implementation here prevents
    CDF, NPPES, Claims, and update fixtures from drifting into length-only
    identifiers that downstream NPI validation rejects.
    """
    body = f"{randomizer.randrange(100_000_000, 1_000_000_000):09d}"
    for check_digit in range(10):
        candidate = body + str(check_digit)
        if is_valid_npi(candidate):
            return candidate
    raise AssertionError("could not generate a valid NPI")


def is_valid_npi(value: str) -> bool:
    """Return whether ``value`` is a correctly formatted, checksum-valid NPI."""
    return value.isdigit() and len(value) == 10 and _is_luhn_valid("80840" + value)


def valid_ssn(randomizer: Random) -> str:
    """Generate a structurally valid nine-digit SSN without a display separator.

    The generator deliberately avoids the disallowed SSA area, group, and
    serial ranges.  It creates synthetic data only; an SSN value is never
    inferred from a real person.
    """
    while True:
        area = randomizer.randrange(1, 900)
        if area == 666:
            continue
        group = randomizer.randrange(1, 100)
        serial = randomizer.randrange(1, 10_000)
        return f"{area:03d}{group:02d}{serial:04d}"


def is_valid_ssn(value: str) -> bool:
    """Return whether ``value`` satisfies the supported SSN structural rules."""
    if not value.isdigit() or len(value) != 9:
        return False
    area, group, serial = int(value[:3]), int(value[3:5]), int(value[5:])
    return area not in {0, 666} and area < 900 and group != 0 and serial != 0


def valid_ein(randomizer: Random) -> str:
    """Generate a realistic nine-digit employer identification number."""
    return f"{randomizer.randrange(1, 100):02d}{randomizer.randrange(1, 10_000_000):07d}"


def valid_phone_number(randomizer: Random) -> str:
    """Generate an unformatted ten-digit North American phone number.

    The schemas store phone values as digits, so this intentionally does not
    insert punctuation.  Area and exchange codes both begin with 2-9.
    """
    return (
        f"{randomizer.randrange(2, 10)}{randomizer.randrange(0, 100):02d}"
        f"{randomizer.randrange(2, 10)}{randomizer.randrange(0, 100):02d}"
        f"{randomizer.randrange(0, 10_000):04d}"
    )


def run_token(seed: int, width: int = 6) -> str:
    """Return a fixed-width, seed-derived token for synthetic business IDs."""
    return f"{abs(seed) % (10**width):0{width}d}"


def _is_luhn_valid(number: str) -> bool:
    """Check a decimal string with the Luhn checksum algorithm."""
    total = 0
    for position, character in enumerate(reversed(number)):
        digit = int(character)
        if position % 2:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return total % 10 == 0
