"""Module entry point for ``python -m healthcare_test_data``.

This module deliberately contains no command-line behavior of its own.  It
delegates to :func:`healthcare_test_data.cli.main` so the installed console
command and module invocation share the same parsing, error handling, and exit
codes.
"""

from healthcare_test_data.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
