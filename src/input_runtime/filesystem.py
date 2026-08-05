"""Durable filesystem repository bundle for the input runtime."""
from __future__ import annotations
from pathlib import Path
from src.storage import StorageConfigType
from .coordination import GLOBAL_SESSION_LOCKS,SessionLockRegistry
from . import _filesystem_cycle
from ._filesystem_cycle import FileSystemActiveCycleSnapshotRepository,FileSystemContextRevisionRepository,FileSystemCycleInboxRepository
from ._filesystem_delivery import FileSystemAgentEmissionRepository,FileSystemFinalizationRepository
from ._filesystem_session import FileSystemInputAdmissionRepository,FileSystemSessionControlRepository,FileSystemSessionInputRuntimeRepository

def _same_inbox_idempotency(existing,incoming):
    return (existing.admission_id,existing.session_id,existing.cycle_id,existing.input_batch_id,existing.generation,existing.payload_size_bytes)==(incoming.admission_id,incoming.session_id,incoming.cycle_id,incoming.input_batch_id,incoming.generation,incoming.payload_size_bytes)
_filesystem_cycle._same_inbox_relation=_same_inbox_idempotency

class FileSystemInputRuntimeRepositories:
    def __init__(self,storage_config:StorageConfigType,*,locks:SessionLockRegistry=GLOBAL_SESSION_LOCKS)->None:
        root=Path(storage_config.root_dir)
        self.sessions=FileSystemSessionInputRuntimeRepository(root=root,locks=locks)
        self.admissions=FileSystemInputAdmissionRepository(root=root,locks=locks)
        self.inbox=FileSystemCycleInboxRepository(root=root,locks=locks)
        self.controls=FileSystemSessionControlRepository(root=root,locks=locks)
        self.snapshots=FileSystemActiveCycleSnapshotRepository(root=root,locks=locks)
        self.context_revisions=FileSystemContextRevisionRepository(root=root,locks=locks)
        self.emissions=FileSystemAgentEmissionRepository(root=root,locks=locks)
        self.finalizations=FileSystemFinalizationRepository(root=root,locks=locks)
