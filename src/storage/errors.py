"""Managed exceptions exposed by the storage layer."""


class StorageError(RuntimeError):
    """Base error for storage operations."""


class StorageNotFoundError(StorageError):
    """Raised when a well-formed storage object does not exist."""


class StorageValidationError(StorageError):
    """Raised when storage input is invalid."""


class StorageIntegrityError(StorageError):
    """Raised when persisted bytes do not match their metadata."""


class StorageSerializationError(StorageError):
    """Raised when storage metadata cannot be serialized or loaded."""


class StorageContentTooLargeError(StorageError):
    """Raised when a full read would exceed the configured memory limit."""


class UnsupportedStorageBackendError(StorageError):
    """Raised when no implementation exists for a configured backend."""
