"""Durable filesystem repository bundle for the input runtime."""

from __future__ import annotations

from pathlib import Path

from src.storage import StorageConfigType

from ._filesystem_identity_recovery_cycle import (
    FileSystemActiveCycleSnapshotRepository,
    FileSystemContextRevisionRepository,
    FileSystemCycleInboxRepository,
)
from ._filesystem_identity_recovery_session import FileSystemInputAdmissionRepository
from ._filesystem_session import FileSystemSessionInputRuntimeRepository
from .coordination import GLOBAL_SESSION_LOCKS, SessionLockRegistry
from .ir5_filesystem_controls import FileSystemSessionControlRepository
from .ir6_delivery_authority import FileSystemAgentEmissionRepository
from .ir7_handoff_ordering import (
    FileSystemFinalizationRepository,
    FileSystemRuntimeHandoffRepository,
)


class FileSystemInputRuntimeRepositories:
    def __init__(
        self,
        storage_config: StorageConfigType,
        *,
        locks: SessionLockRegistry = GLOBAL_SESSION_LOCKS,
    ) -> None:
        root = Path(storage_config.root_dir)
        self.root = root
        self.locks = locks
        self.sessions = FileSystemSessionInputRuntimeRepository(
            root=root,
            locks=locks,
        )
        self.admissions = FileSystemInputAdmissionRepository(
            root=root,
            locks=locks,
        )
        self.inbox = FileSystemCycleInboxRepository(root=root, locks=locks)
        self.handoffs = FileSystemRuntimeHandoffRepository(
            root=root,
            locks=locks,
        )
        self.controls = FileSystemSessionControlRepository(
            root=root,
            locks=locks,
        )
        self.snapshots = FileSystemActiveCycleSnapshotRepository(
            root=root,
            locks=locks,
        )
        self.context_revisions = FileSystemContextRevisionRepository(
            root=root,
            locks=locks,
        )
        self.emissions = FileSystemAgentEmissionRepository(
            root=root,
            locks=locks,
        )
        self.finalizations = FileSystemFinalizationRepository(
            root=root,
            locks=locks,
            handoffs=self.handoffs,
        )
