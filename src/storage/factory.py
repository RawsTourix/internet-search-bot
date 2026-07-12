"""Storage dependency container and backend factory."""

from dataclasses import dataclass

from .config import StorageConfigType
from .errors import UnsupportedStorageBackendError
from .file_backend import (
    FileSystemArtifactStore,
    FileSystemContentStore,
    _AtomicFileBackend,
)
from .interfaces import ArtifactStore, ContentStore


@dataclass(slots=True)
class StorageServices:
    content_store: ContentStore
    artifact_store: ArtifactStore


def create_storage_services(config: StorageConfigType) -> StorageServices:
    """Create storage implementations for the configured backend."""
    if config.backend != "filesystem":
        raise UnsupportedStorageBackendError(
            f"Unsupported storage backend {config.backend!r}"
        )
    backend = _AtomicFileBackend(config)
    return StorageServices(
        content_store=FileSystemContentStore(backend),
        artifact_store=FileSystemArtifactStore(backend),
    )
