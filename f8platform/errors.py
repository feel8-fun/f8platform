"""Domain errors shared by platform clients and API adapters."""

class InvalidRequestError(ValueError):
    """A rejected domain input that can be reported to a caller."""


class NotFoundError(FileNotFoundError):
    """A requested resource does not exist."""


class ConflictError(RuntimeError):
    """An action conflicts with current lifecycle state."""


class ServiceUnavailableError(RuntimeError):
    """A managed process could not become available."""
