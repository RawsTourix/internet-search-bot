"""Input-runtime domain and configuration errors."""


class InputRuntimeError(Exception):
    """Base exception for input-runtime contracts."""


class InputRuntimeConfigValidationError(InputRuntimeError, ValueError):
    """Raised when the input-runtime configuration is invalid."""


class InputRuntimeConflictError(InputRuntimeError):
    """Raised when optimistic concurrency or claim fencing fails."""


class InputRuntimeNotFoundError(InputRuntimeError):
    """Raised when an authoritative record does not exist."""
