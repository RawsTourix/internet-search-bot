"""Emission and finalization filesystem repositories."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .errors import InputRuntimeConflictError, InputRuntimeNotFoundError
from .models import (
    AgentEmission, CycleFinalizationRecord, EmissionState, FinalizationState,
)
from .serialization import atomic_write_model, read_model, storage_key
from ._filesystem_common import _DeliveryClaim, _RepositoryBase, validated_copy


class FileSystemAgentEmissionRepository(_RepositoryBase):
    async def _all(self) -> tuple[AgentEmission, ...]:
        cycles = self.layout.root / "cycles"
        records = []
        if cycles.exists():
            for path in sorted(cycles.glob("*/emissions/*.json")):
                if path.name.endswith(".claim.json"):
                    continue
                records.append(read_model(path, AgentEmission))
        return tuple(records)

    async def create_if_absent(self, emission: AgentEmission) -> AgentEmission:
        async with self.locks.hold(self.root, emission.session_id):
            existing = await self.get_by_idempotency_key(emission.cycle_id, emission.idempotency_key)
            if existing is not None:
                return existing
            atomic_write_model(self.layout.emission(emission.cycle_id, emission.emission_id), emission)
            return emission

    async def get_by_idempotency_key(self, cycle_id: str, idempotency_key: str) -> AgentEmission | None:
        return next((item for item in await self._all() if item.cycle_id == cycle_id and item.idempotency_key == idempotency_key.strip()), None)

    async def _find(self, emission_id: str) -> tuple[Path, AgentEmission]:
        cycles = self.layout.root / "cycles"
        if cycles.exists():
            name = f"{storage_key(emission_id)}.json"
            for path in sorted(cycles.glob(f"*/emissions/{name}")):
                return path, read_model(path, AgentEmission)
        raise InputRuntimeNotFoundError(emission_id)

    async def claim_delivery(self, emission_id: str, *, claim_token: str) -> AgentEmission:
        path, record = await self._find(emission_id)
        async with self.locks.hold(self.root, record.session_id):
            current = read_model(path, AgentEmission)
            if current.state != EmissionState.READY:
                raise InputRuntimeConflictError("emission is not ready")
            token = claim_token.strip()
            if not token:
                raise ValueError("claim_token must not be empty")
            updated = validated_copy(current, state=EmissionState.DELIVERING)
            atomic_write_model(path, updated)
            atomic_write_model(self.layout.emission_claim(current.cycle_id, current.emission_id), _DeliveryClaim(emission_id=current.emission_id, claim_token=token))
            return updated

    async def complete_delivery(self, emission_id: str, *, claim_token: str, delivered_at: datetime) -> AgentEmission:
        path, record = await self._find(emission_id)
        async with self.locks.hold(self.root, record.session_id):
            current = read_model(path, AgentEmission)
            claim_path = self.layout.emission_claim(current.cycle_id, current.emission_id)
            claim = read_model(claim_path, _DeliveryClaim)
            if current.state != EmissionState.DELIVERING or claim.claim_token != claim_token.strip():
                raise InputRuntimeConflictError("stale emission delivery claim")
            updated = validated_copy(current, state=EmissionState.DELIVERED, delivered_at=delivered_at, error_code=None)
            atomic_write_model(path, updated)
            claim_path.unlink(missing_ok=True)
            return updated

    async def fail_delivery(self, emission_id: str, *, claim_token: str, state: str, error_code: str) -> AgentEmission:
        path, record = await self._find(emission_id)
        next_state = EmissionState(state)
        if next_state not in {EmissionState.FAILED, EmissionState.UNKNOWN}:
            raise ValueError("delivery failure state must be failed or unknown")
        async with self.locks.hold(self.root, record.session_id):
            current = read_model(path, AgentEmission)
            claim_path = self.layout.emission_claim(current.cycle_id, current.emission_id)
            claim = read_model(claim_path, _DeliveryClaim)
            if current.state != EmissionState.DELIVERING or claim.claim_token != claim_token.strip():
                raise InputRuntimeConflictError("stale emission delivery claim")
            updated = validated_copy(current, state=next_state, error_code=error_code)
            atomic_write_model(path, updated)
            claim_path.unlink(missing_ok=True)
            return updated

    async def list_pending_delivery(self) -> tuple[AgentEmission, ...]:
        return tuple(item for item in await self._all() if item.state in {EmissionState.READY, EmissionState.DELIVERING})

    async def cancel_generation(self, session_id: str, *, generation: int, reason_code: str) -> tuple[AgentEmission, ...]:
        changed = []
        for record in await self._all():
            if record.session_id == session_id and record.state == EmissionState.READY:
                path, _ = await self._find(record.emission_id)
                async with self.locks.hold(self.root, session_id):
                    updated = validated_copy(record, state=EmissionState.CANCELLED, error_code=None)
                    atomic_write_model(path, updated)
                    changed.append(updated)
        return tuple(changed)


class FileSystemFinalizationRepository(_RepositoryBase):
    async def _all(self) -> tuple[CycleFinalizationRecord, ...]:
        cycles = self.layout.root / "cycles"
        records = []
        if cycles.exists():
            for path in sorted(cycles.glob("*/finalizations/*.json")):
                records.append(read_model(path, CycleFinalizationRecord))
        return tuple(records)

    async def prepare(self, record: CycleFinalizationRecord) -> CycleFinalizationRecord:
        if record.state != FinalizationState.PREPARED:
            raise ValueError("prepare requires PREPARED state")
        async with self.locks.hold(self.root, record.session_id):
            path = self.layout.finalization(record.cycle_id, record.finalization_id)
            if path.exists():
                return read_model(path, CycleFinalizationRecord)
            atomic_write_model(path, record)
            return record

    async def get(self, finalization_id: str) -> CycleFinalizationRecord | None:
        for record in await self._all():
            if record.finalization_id == finalization_id:
                return record
        return None

    async def _transition(self, finalization_id: str, *, expected_state: str, next_record: CycleFinalizationRecord) -> CycleFinalizationRecord:
        current = await self.get(finalization_id)
        if current is None:
            raise InputRuntimeNotFoundError(finalization_id)
        async with self.locks.hold(self.root, current.session_id):
            path = self.layout.finalization(current.cycle_id, finalization_id)
            current = read_model(path, CycleFinalizationRecord)
            if current.state != FinalizationState(expected_state):
                raise InputRuntimeConflictError("stale finalization state")
            if next_record.finalization_id != current.finalization_id or next_record.cycle_id != current.cycle_id or next_record.session_id != current.session_id:
                raise InputRuntimeConflictError("finalization identity changed")
            atomic_write_model(path, next_record)
            return next_record

    async def advance(self, finalization_id: str, *, expected_state: str, next_record: CycleFinalizationRecord) -> CycleFinalizationRecord:
        return await self._transition(finalization_id, expected_state=expected_state, next_record=next_record)

    async def abort(self, finalization_id: str, *, expected_state: str, next_record: CycleFinalizationRecord) -> CycleFinalizationRecord:
        if next_record.state not in {FinalizationState.ABORTED_NEW_INPUT, FinalizationState.ABORTED_CONTROL, FinalizationState.FAILED_RECOVERABLE, FinalizationState.FAILED_TERMINAL}:
            raise ValueError("abort requires an aborted or failed state")
        return await self._transition(finalization_id, expected_state=expected_state, next_record=next_record)

    async def list_recoverable(self) -> tuple[CycleFinalizationRecord, ...]:
        recoverable = {FinalizationState.PREPARED, FinalizationState.RESULT_PERSISTED, FinalizationState.OUTPUT_READY, FinalizationState.FAILED_RECOVERABLE}
        return tuple(item for item in await self._all() if item.state in recoverable)

    async def cancel_generation(self, session_id: str, *, generation: int, reason_code: str) -> tuple[CycleFinalizationRecord, ...]:
        changed = []
        for record in await self._all():
            if record.session_id == session_id and record.generation == generation and record.state not in {FinalizationState.TERMINAL_COMMITTED, FinalizationState.ABORTED_NEW_INPUT, FinalizationState.ABORTED_CONTROL, FinalizationState.FAILED_TERMINAL}:
                updated = validated_copy(record, state=FinalizationState.ABORTED_CONTROL, updated_at=datetime.now(timezone.utc), failure_code=None)
                async with self.locks.hold(self.root, session_id):
                    atomic_write_model(self.layout.finalization(record.cycle_id, record.finalization_id), updated)
                changed.append(updated)
        return tuple(changed)
