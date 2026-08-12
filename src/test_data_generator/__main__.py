"""Module entry point for ``python -m test_data_generator``.

This module deliberately contains no command-line behavior of its own.  It
delegates to :func:`test_data_generator.cli.main` so the installed console
command and module invocation share the same parsing, error handling, and exit
codes.
"""

from test_data_generator.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
