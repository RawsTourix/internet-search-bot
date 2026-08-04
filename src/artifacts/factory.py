"""Artifact service container and filesystem factory."""

from dataclasses import dataclass

from ..storage.config import StorageConfigType
from ..storage.interfaces import ContentStore
from .advanced_delivery import (
    AdvancedArtifactDeliveryService,
    AdvancedFileSystemArtifactDeliveryStore,
)
from .candidate_store import (
    ArtifactCandidateStore,
    FileSystemArtifactCandidateStore,
)
from .config import ArtifactConfigType
from .delivery import (
    ArtifactDeliveryService,
    FileSystemArtifactDeliveryStore,
)
from .format_registry import ArtifactFormatRegistry, build_default_format_registry
from .interfaces import ArtifactStore
from .promotion import ArtifactCandidatePromotionService
from .resilient_file_store import ResilientFileSystemArtifactStore
from .service import ArtifactService
from .tracing import ArtifactTraceService, FileSystemArtifactTraceStore
from .workspace import ArtifactWorkspaceManager


@dataclass(slots=True)
class ArtifactServices:
    config: ArtifactConfigType
    artifact_store: ArtifactStore
    candidate_store: ArtifactCandidateStore
    delivery_store: FileSystemArtifactDeliveryStore
    trace_store: FileSystemArtifactTraceStore
    format_registry: ArtifactFormatRegistry
    artifact_service: ArtifactService
    promotion_service: ArtifactCandidatePromotionService
    delivery_service: ArtifactDeliveryService
    trace_service: ArtifactTraceService
    workspace_manager: ArtifactWorkspaceManager


def create_artifact_services(
    *,
    storage_config: StorageConfigType,
    artifact_config: ArtifactConfigType,
    content_store: ContentStore,
    allow_legacy_layout: bool = False,
) -> ArtifactServices:
    """Create the artifact domain independently from legacy storage APIs."""
    trace_store = FileSystemArtifactTraceStore(
        storage_config,
        max_file_bytes=artifact_config.trace_max_file_bytes,
    )
    trace_service = ArtifactTraceService(
        trace_store,
        enabled=artifact_config.trace_enabled,
        max_string_chars=artifact_config.trace_max_string_chars,
    )
    artifact_store = ResilientFileSystemArtifactStore(
        storage_config=storage_config,
        artifact_config=artifact_config,
        content_store=content_store,
        allow_legacy_layout=allow_legacy_layout,
    )
    candidate_store = FileSystemArtifactCandidateStore(storage_config)
    delivery_store = AdvancedFileSystemArtifactDeliveryStore(
        storage_config,
        trace_service=trace_service,
    )
    format_registry = build_default_format_registry()
    artifact_service = ArtifactService(
        config=artifact_config,
        artifact_store=artifact_store,
        content_store=content_store,
        format_registry=format_registry,
    )
    promotion_service = ArtifactCandidatePromotionService(
        artifact_service=artifact_service,
        candidate_store=candidate_store,
    )
    delivery_service = AdvancedArtifactDeliveryService(
        config=artifact_config,
        artifact_service=artifact_service,
        content_store=content_store,
        store=delivery_store,
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
        delivery_store=delivery_store,
        trace_store=trace_store,
        format_registry=format_registry,
        artifact_service=artifact_service,
        promotion_service=promotion_service,
        delivery_service=delivery_service,
        trace_service=trace_service,
        workspace_manager=workspace_manager,
    )
