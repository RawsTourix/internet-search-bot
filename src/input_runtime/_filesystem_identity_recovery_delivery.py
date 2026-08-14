"""Crash-recoverable emission and finalization repositories."""

from __future__ import annotations

from . import _filesystem_identity as identity_module
from ._filesystem_delivery import _final_identity, _same_emission_relation
from ._filesystem_identity import (
    FileSystemAgentEmissionRepository as _EmissionIdentityBase,
    FileSystemFinalizationRepository as _FinalizationIdentityBase,
)
from ._filesystem_identity_recovery_common import (
    recover_cycle_authority,
    recover_indexed,
    scan_models,
)
from .errors import InputRuntimeConflictError
from .models import (
    AgentEmission,
    CycleFinalizationRecord,
    FinalizationState,
)
from .serialization import list_models


def atomic_write_model(path, model):
    """Keep the existing identity-module write seam used by crash tests."""
    return identity_module.atomic_write_model(path, model)


class FileSystemAgentEmissionRepository(_EmissionIdentityBase):
    """Emission writes with recoverable idempotency and identity indexes."""

    def _scan_all(self) -> tuple[AgentEmission, ...]:
        return scan_models(
            self.layout.root.glob("cycles/*/emissions/*.json"),
            AgentEmission,
            identity_name="emission",
        )

    def _restore_indexes(self, emission: AgentEmission) -> None:
        recover_cycle_authority(
            self,
            emission.cycle_id,
            emission.session_id,
        )
        self._index(emission)

    async def get_by_idempotency_key(
        self,
        cycle_id: str,
        idempotency_key: str,
    ) -> AgentEmission | None:
        return next(
            (
                item
                for item in list_models(
                    self.layout.emissions(cycle_id),
                    AgentEmission,
                )
                if item.idempotency_key == idempotency_key.strip()
            ),
            None,
        )

    async def create_if_absent(
        self,
        emission: AgentEmission,
    ) -> AgentEmission:
        async with self.locks.hold_identity_then_session(
            self.root,
            emission.session_id,
        ):
            existing = await self.get_by_idempotency_key(
                emission.cycle_id,
                emission.idempotency_key,
            )
            if existing is not None:
                if not _same_emission_relation(existing, emission):
                    raise InputRuntimeConflictError(
                        "emission idempotency relation changed"
                    )
                self._restore_indexes(existing)
                return existing

            by_id = recover_indexed(
                self,
                self.layout.record_index(
                    "emission",
                    emission.emission_id,
                ),
                AgentEmission,
                identity_name="emission",
                matches_identity=lambda item: item.emission_id == emission.emission_id,
                scan=self._scan_all,
                restore=self._restore_indexes,
            )
            if by_id is not None:
                if by_id != emission:
                    raise InputRuntimeConflictError(
                        "emission stable ID collision"
                    )
                self._restore_indexes(by_id)
                return by_id

            recover_cycle_authority(
                self,
                emission.cycle_id,
                emission.session_id,
            )
            atomic_write_model(
                self.layout.emission(
                    emission.cycle_id,
                    emission.emission_id,
                ),
                emission,
            )
            self._index(emission)
            return emission


class FileSystemFinalizationRepository(_FinalizationIdentityBase):
    """Finalization writes with recoverable stable and cycle indexes."""

    def _scan_all(self) -> tuple[CycleFinalizationRecord, ...]:
        return scan_models(
            self.layout.root.glob("cycles/*/finalizations/*.json"),
            CycleFinalizationRecord,
            identity_name="finalization",
        )

    def _restore_indexes(self, record: CycleFinalizationRecord) -> None:
        recover_cycle_authority(
            self,
            record.cycle_id,
            record.session_id,
        )
        self._index(record)

    async def prepare(
        self,
        record: CycleFinalizationRecord,
    ) -> CycleFinalizationRecord:
        if record.state != FinalizationState.PREPARED:
            raise ValueError("prepare requires PREPARED")
        async with self.locks.hold_identity_then_session(
            self.root,
            record.session_id,
        ):
            existing = recover_indexed(
                self,
                self.layout.record_index(
                    "finalization",
                    record.finalization_id,
                ),
                CycleFinalizationRecord,
                identity_name="finalization",
                matches_identity=lambda item: (
                    item.finalization_id == record.finalization_id
                ),
                scan=self._scan_all,
                restore=self._restore_indexes,
            )
            if existing is not None:
                if _final_identity(existing) != _final_identity(record):
                    raise InputRuntimeConflictError(
                        "finalization stable ID collision"
                    )
                if existing != record:
                    raise InputRuntimeConflictError(
                        "finalization state already advanced"
                    )
                self._restore_indexes(existing)
                return existing

            recover_cycle_authority(
                self,
                record.cycle_id,
                record.session_id,
            )
            atomic_write_model(
                self.layout.finalization(
                    record.cycle_id,
                    record.finalization_id,
                ),
                record,
            )
            self._index(record)
            return record
