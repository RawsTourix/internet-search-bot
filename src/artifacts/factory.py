"""Artifact service container and filesystem factory."""

from dataclasses import dataclass

from ..storage.config import StorageConfigType
from ..storage.interfaces import ContentStore
from .candidate_store import (
    ArtifactCandidateStore,
    FileSystemArtifactCandidateStore,
)
from .config import ArtifactConfigType
from .file_store import FileSystemArtifactStore
from .format_registry import ArtifactFormatRegistry, build_default_format_registry
from .interfaces import ArtifactStore
from .service import ArtifactService
from .workspace import ArtifactWorkspaceManager


@dataclass(slots=True)
class ArtifactServices:
    config: ArtifactConfigType
    artifact_store: ArtifactStore
    candidate_store: ArtifactCandidateStore
    format_registry: ArtifactFormatRegistry
    artifact_service: ArtifactService
    workspace_manager: ArtifactWorkspaceManager


def create_artifact_services(
    *,
    storage_config: StorageConfigType,
    artifact_config: ArtifactConfigType,
    content_store: ContentStore,
    allow_legacy_layout: bool = False,
) -> ArtifactServices:
    """Create the artifact domain independently from legacy storage APIs."""
    artifact_store = FileSystemArtifactStore(
        storage_config=storage_config,
        artifact_config=artifact_config,
        content_store=content_store,
        allow_legacy_layout=allow_legacy_layout,
    )
    candidate_store = FileSystemArtifactCandidateStore(storage_config)
    format_registry = build_default_format_registry()
    artifact_service = ArtifactService(
        config=artifact_config,
        artifact_store=artifact_store,
        content_store=content_store,
        format_registry=format_registry,
    )
    workspace_manager = ArtifactWorkspaceManager(
        storage_config=storage_config,
        artifact_config=artifact_config,
        artifact_service=artifact_service,
        content_store=content_store,
        candidate_store=candidate_store,
        format_registry=format_registry,
    )
    return ArtifactServices(
        config=artifact_config,
        artifact_store=artifact_store,
        candidate_store=candidate_store,
        format_registry=format_registry,
        artifact_service=artifact_service,
        workspace_manager=workspace_manager,
    )
