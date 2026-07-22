"""Public storage foundation API."""

from .config import StorageConfigType
from .errors import (
    StorageContentTooLargeError,
    StorageError,
    StorageIntegrityError,
    StorageNotFoundError,
    StorageSerializationError,
    StorageStreamSourceError,
    StorageValidationError,
    UnsupportedStorageBackendError,
)
from .factory import StorageServices, create_storage_services
from .interfaces import ArtifactStore, ContentStore
from .models import (
    ArtifactRef,
    ContentMatch,
    ContentMetadata,
    ContentRange,
    ContentRef,
    SummaryStatus,
    StoredResultRef,
)

__all__ = [
    "ArtifactRef",
    "ArtifactStore",
    "ContentMatch",
    "ContentMetadata",
    "ContentRange",
    "ContentRef",
    "ContentStore",
    "StorageConfigType",
    "StorageContentTooLargeError",
    "StorageError",
    "StorageIntegrityError",
    "StorageNotFoundError",
    "StorageSerializationError",
    "StorageServices",
    "StorageStreamSourceError",
    "StorageValidationError",
    "SummaryStatus",
    "StoredResultRef",
    "UnsupportedStorageBackendError",
    "create_storage_services",
]
