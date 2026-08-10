"""Public package interface for configuration-driven healthcare test data.

The package reads a small JSON configuration, produces deterministic,
source-shaped JSONL records, and validates every record before it is written.
Only :func:`generate` is exported as the supported programmatic entry point;
the remaining modules contain the configuration, planning, and output details.
"""

from healthcare_test_data.cli import generate

__version__ = "0.1.0"

__all__ = ["generate"]
