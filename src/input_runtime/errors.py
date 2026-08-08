"""Input-runtime domain and configuration errors."""


class InputRuntimeError(Exception):
    """Base exception for input-runtime contracts."""


class InputRuntimeConfigValidationError(InputRuntimeError, ValueError):
    """Raised when the input-runtime configuration is invalid."""


class InputRuntimeConflictError(InputRuntimeError):
    """Raised when optimistic concurrency or claim fencing fails."""


class InputAdmissionDecisionStaleError(InputRuntimeConflictError):
    """Raised when an optimistic admission classification lost durable ordering."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"stale admission decision: {reason_code}")


class InputRuntimeNotFoundError(InputRuntimeError):
    """Raised when an authoritative record does not exist."""
