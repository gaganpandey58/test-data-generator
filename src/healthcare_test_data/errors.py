"""Domain errors raised before healthcare data generation begins."""


class ConfigurationError(ValueError):
    """Indicate that the root generation configuration is invalid."""


class GenerationError(RuntimeError):
    """Indicate a generation failure whose message is safe for CLI output."""
