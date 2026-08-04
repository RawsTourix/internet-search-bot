"""Composition root for the filesystem ingress foundation."""

from dataclasses import dataclass
from pathlib import Path

from ..artifacts import ArtifactServices
from ..storage import StorageConfigType
from ..storage.interfaces import ContentStore
from ..interaction.capabilities import build_default_capability_registry
from ..interaction.capability_store import FileSystemCapabilitySnapshotStore
from ..interaction.config import InteractionConfig
from ..interaction.presentation_service import InputPresentationCoordinator
from ..interaction.presentation_store import FileSystemInputPresentationStore
from ..localization.service import LocalizationService
from .collection_store import FileSystemInputCollectionStore
from .config import IngressConfigType
from .draft_control import InputDraftControlService
from .explicit_control import ExplicitInputDraftControlService
from .explicit_service import ExplicitCollectionIngressService
from .explicit_store import (
    ExplicitCollectionInputBatchStore,
    FileSystemExplicitInputCollectionStore,
)
from .service import ArtifactIngressService
from .store import FileSystemIngressEventStore, FileSystemInputBatchStore


@dataclass(slots=True)
class IngressServices:
    config: IngressConfigType
    event_store: FileSystemIngressEventStore
    batch_store: FileSystemInputBatchStore
    ingress_service: ArtifactIngressService
    collection_store: FileSystemInputCollectionStore | None = None
    draft_control_service: InputDraftControlService | None = None
    capability_store: FileSystemCapabilitySnapshotStore | None = None
    presentation_store: FileSystemInputPresentationStore | None = None
    localization_service: LocalizationService | None = None


def create_ingress_services(
    *,
    storage_config: StorageConfigType,
    ingress_config: IngressConfigType,
    content_store: ContentStore,
    artifact_services: ArtifactServices,
    interaction_config: InteractionConfig | None = None,
) -> IngressServices:
    interaction = interaction_config or InteractionConfig()
    event_store = FileSystemIngressEventStore(storage_config)
    collection_store = FileSystemExplicitInputCollectionStore(storage_config)
    batch_store = ExplicitCollectionInputBatchStore(
        storage_config,
        ingress_config,
        collection_store=collection_store,
    )
    registry = build_default_capability_registry(
        interaction.client_capabilities.contract_version
    )
    capability_store = FileSystemCapabilitySnapshotStore(
        storage_config,
        registry,
        interaction.client_capabilities,
    )
    storage_root = Path(storage_config.root_dir).expanduser()
    if not storage_root.is_absolute():
        storage_root = Path.cwd() / storage_root
    presentation_store = FileSystemInputPresentationStore(
        storage_root.resolve(strict=False),
        atomic_writes=storage_config.atomic_writes,
    )
    presentation_coordinator = (
        InputPresentationCoordinator(
            presentation_store,
            config=interaction.input_presentation,
        )
        if interaction.input_presentation.enabled
        else None
    )
    draft_control_service = ExplicitInputDraftControlService(
        event_store=event_store,
        batch_store=batch_store,
        collection_store=collection_store,
        presentation_coordinator=presentation_coordinator,
        idle_timeout_seconds=(
            ingress_config.explicit_collection_idle_timeout_seconds
        ),
    )
    localization_service = LocalizationService.from_directory(
        config=interaction.localization
    )
    ingress_service = ExplicitCollectionIngressService(
        config=ingress_config,
        artifact_config=artifact_services.config,
        content_store=content_store,
        artifact_services=artifact_services,
        event_store=event_store,
        batch_store=batch_store,
        collection_store=collection_store,
        draft_control_service=draft_control_service,
        capability_store=capability_store,
        localization_service=localization_service,
        presentation_coordinator=presentation_coordinator,
        telegram_document_grouping=(
            interaction.telegram_output.prefer_document_groups
        ),
        telegram_message_editing=(
            interaction.telegram_output.status_message_editing
        ),
    )
    return IngressServices(
        config=ingress_config,
        event_store=event_store,
        batch_store=batch_store,
        ingress_service=ingress_service,
        collection_store=collection_store,
        draft_control_service=draft_control_service,
        capability_store=capability_store,
        presentation_store=presentation_store,
        localization_service=localization_service,
    )
