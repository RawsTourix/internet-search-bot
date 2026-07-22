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
    """Raised when a stream or full read exceeds a configured hard limit."""


class StorageStreamSourceError(StorageError):
    """Typed upstream iterator failure that must not be rewrapped by storage."""


class UnsupportedStorageBackendError(StorageError):
    """Raised when no implementation exists for a configured backend."""
