"""Composition root for the filesystem ingress foundation."""

from dataclasses import dataclass

from ..artifacts import ArtifactServices
from ..storage import StorageConfigType
from ..storage.interfaces import ContentStore
from .config import IngressConfigType
from .service import ArtifactIngressService
from .store import FileSystemIngressEventStore, FileSystemInputBatchStore


@dataclass(slots=True)
class IngressServices:
    config: IngressConfigType
    event_store: FileSystemIngressEventStore
    batch_store: FileSystemInputBatchStore
    ingress_service: ArtifactIngressService


def create_ingress_services(
    *,
    storage_config: StorageConfigType,
    ingress_config: IngressConfigType,
    content_store: ContentStore,
    artifact_services: ArtifactServices,
) -> IngressServices:
    event_store = FileSystemIngressEventStore(storage_config)
    batch_store = FileSystemInputBatchStore(storage_config)
    ingress_service = ArtifactIngressService(
        config=ingress_config,
        artifact_config=artifact_services.config,
        content_store=content_store,
        artifact_services=artifact_services,
        event_store=event_store,
        batch_store=batch_store,
    )
    return IngressServices(
        config=ingress_config,
        event_store=event_store,
        batch_store=batch_store,
        ingress_service=ingress_service,
    )
