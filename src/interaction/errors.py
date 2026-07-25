"""Typed errors exposed by the interaction runtime."""


class InteractionError(RuntimeError):
    """Base class for transport-independent interaction failures."""


class InteractionValidationError(InteractionError):
    """An interaction contract failed deterministic validation."""


class InteractionConflictError(InteractionError):
    """An immutable or idempotent interaction conflicts with stored state."""


class InteractionNotFoundError(InteractionError):
    """An interaction entity does not exist under the current authority."""


class InteractionStorageError(InteractionError):
    """The interaction store could not durably persist or read state."""


class InteractionIntegrityError(InteractionError):
    """Stored interaction metadata is corrupt or unsafe."""


class CapabilityValidationError(InteractionValidationError):
    """A client capability declaration is invalid."""


class CapabilityConflictError(InteractionConflictError):
    """A client capability snapshot conflicts with an existing binding."""


class CapabilityNotFoundError(InteractionNotFoundError):
    """A capability snapshot is unknown."""


class PresentationConflictError(InteractionConflictError):
    """An InputBatch presentation transition is invalid."""


class PresentationNotFoundError(InteractionNotFoundError):
    """An InputBatch presentation does not exist."""


class OutputBatchConflictError(InteractionConflictError):
    """An OutputBatch idempotency or state transition conflict occurred."""


class OutputBatchNotFoundError(InteractionNotFoundError):
    """An OutputBatch or output attempt does not exist."""
