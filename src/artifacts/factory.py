"""Artifact service container and filesystem factory."""

from dataclasses import dataclass

from ..storage.config import StorageConfigType
from ..storage.interfaces import ContentStore
from .config import ArtifactConfigType
from .file_store import FileSystemArtifactStore
from .interfaces import ArtifactStore


@dataclass(slots=True)
class ArtifactServices:
    config: ArtifactConfigType
    artifact_store: ArtifactStore


def create_artifact_services(
    *,
    storage_config: StorageConfigType,
    artifact_config: ArtifactConfigType,
    content_store: ContentStore,
    allow_legacy_layout: bool = False,
) -> ArtifactServices:
    """Create the artifact domain independently from legacy storage APIs."""
    return ArtifactServices(
        config=artifact_config,
        artifact_store=FileSystemArtifactStore(
            storage_config=storage_config,
            artifact_config=artifact_config,
            content_store=content_store,
            allow_legacy_layout=allow_legacy_layout,
        ),
    )
