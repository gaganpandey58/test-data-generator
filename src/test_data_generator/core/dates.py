"""Date defaults shared by generated healthcare records."""

from datetime import date


def current_ingestion_date() -> str:
    """Return today's compact date in the schema-required ``YYYYMMDD`` format.

    Exact fixture dates remain configurable through ``generation.ingestion_dates``.
    Keeping the default here dynamic prevents generated production-like data from
    silently carrying a historical fixed ingestion date.
    """
    return date.today().strftime("%Y%m%d")
