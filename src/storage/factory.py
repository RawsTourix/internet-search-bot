"""Storage dependency container and backend factory."""

from dataclasses import dataclass

from .config import StorageConfigType
from .errors import UnsupportedStorageBackendError
from .file_backend import (
    FileSystemArtifactStore,
    _AtomicFileBackend,
)
from .interfaces import ArtifactStore, ContentStore
from .streaming import StreamingFileSystemContentStore


@dataclass(slots=True)
class StorageServices:
    config: StorageConfigType
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
        config=config,
        content_store=StreamingFileSystemContentStore(backend),
        artifact_store=FileSystemArtifactStore(backend),
    )
