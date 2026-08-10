"""Define safe domain errors for configuration and generation boundaries.

The CLI uses these exceptions to distinguish expected user-correctable errors
from unexpected implementation failures and to avoid printing tracebacks or
potentially sensitive generated data during ordinary command execution.
"""


class ConfigurationError(ValueError):
    """Indicate that the supplied generation configuration is invalid.

    This error covers malformed JSON, invalid configuration schema, unsafe
    output paths, unsupported profiles or scenarios, and invalid enabled-entity
    relationships. Its message is designed to be suitable for CLI output.
    """


class GenerationError(RuntimeError):
    """Indicate a generation failure whose message is safe for CLI output.

    The engine raises this error when an approved entity generator or schema
    cannot be loaded, or when a generated record violates its schema. The CLI
    adds entity context before returning a non-zero exit status.
    """
