"""Durable admission boundary for every authoritative committed InputBatch."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .admission import (
    AdmissionWakeCoordinator,
    CommittedInputBatchReader,
    InputAdmissionAction,
    InputAdmissionOutcome,
)
from .config import InputRuntimeConfigType
from .errors import InputRuntimeConflictError
from .factory import InputRuntimeRepositories
from .models import (
    AdmissionKind,
    AdmissionState,
    ClaimedInboxRange,
    CycleInboxItem,
    CycleStatus,
    InboxState,
    InputAdmissionRecord,
    SessionInputRuntimeState,
)


Clock = Callable[[], datetime]
CycleIdFactory = Callable[[], str]
PayloadSizeResolver = Callable[[Any], int]


_ACTION_BY_STATUS: dict[CycleStatus, tuple[AdmissionKind, InputAdmissionAction]] = {
    CycleStatus.RUNNING: (
        AdmissionKind.CONTINUE_RUNNING,
        InputAdmissionAction.QUEUED_RUNNING,
    ),
    CycleStatus.FINALIZING: (
        AdmissionKind.CONTINUE_RUNNING,
        InputAdmissionAction.QUEUED_RUNNING,
    ),
    CycleStatus.WAITING_USER: (
        AdmissionKind.RESUME_WAITING,
        InputAdmissionAction.RESUME_WAITING,
    ),
    CycleStatus.PAUSE_REQUESTED: (
        AdmissionKind.QUEUE_PAUSED,
        InputAdmissionAction.QUEUED_PAUSED,
    ),
    CycleStatus.PAUSED_BY_USER: (
        AdmissionKind.QUEUE_PAUSED,
        InputAdmissionAction.QUEUED_PAUSED,
    ),
    CycleStatus.INTERRUPTED: (
        AdmissionKind.RESUME_INTERRUPTED,
        InputAdmissionAction.RESUME_INTERRUPTED,
    ),
}
_TERMINAL_OR_IDLE = {
    CycleStatus.IDLE,
    CycleStatus.DONE,
    CycleStatus.ERROR,
    CycleStatus.CANCELLED,
}
_PENDING_INBOX_STATES = {
    InboxState.QUEUED,
    InboxState.CLAIMED,
    InboxState.APPLYING,
}


class InputAdmissionService:
    """Coordinate committed-batch admission without transport/runtime imports."""

    def __init__(
        self,
        *,
        config: InputRuntimeConfigType,
        repositories: InputRuntimeRepositories,
        committed_batches: CommittedInputBatchReader,
        wake_coordinator: AdmissionWakeCoordinator,
        cycle_id_factory: CycleIdFactory | None = None,
        clock: Clock | None = None,
        payload_size_resolver: PayloadSizeResolver | None = None,
    ) -> None:
        self.config = config
        self.repositories = repositories
        self.committed_batches = committed_batches
        self.wake_coordinator = wake_coordinator
        self.cycle_id_factory = cycle_id_factory or (lambda: uuid4().hex)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.payload_size_resolver = (
            payload_size_resolver or self._default_payload_size
        )

    @staticmethod
    def _default_payload_size(batch: Any) -> int:
        serializer = getattr(batch, "model_dump_json", None)
        if not callable(serializer):
            raise InputRuntimeConflictError(
                "committed batch payload metadata is unavailable"
            )
        metadata_bytes = len(serializer().encode("utf-8"))
        manifest = getattr(batch, "artifact_manifest", None)
        items = tuple(getattr(manifest, "items", ()) or ())
        artifact_bytes = 0
        for item in items:
            size = getattr(item, "size_bytes", None)
            if size is None or int(size) < 0:
                raise InputRuntimeConflictError(
                    "committed batch artifact size metadata is unavailable"
                )
            artifact_bytes += int(size)
        return metadata_bytes + artifact_bytes

    @asynccontextmanager
    async def _hold_admission(self, session_id: str):
        root = self.repositories.coordination_root
        locks = self.repositories.coordination_locks
        if root is None or locks is None:
            yield
            return
        async with locks.hold_admission(root, session_id):
            yield

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("runtime clock must return timezone-aware datetime")
        return value.astimezone(timezone.utc)

    async def _load_authoritative_batch(
        self,
        input_batch_id: str,
        *,
        session_id: str,
    ) -> Any:
        batch = await self.committed_batches.get_committed(input_batch_id)
        if str(getattr(batch, "input_batch_id", "")) != input_batch_id:
            raise InputRuntimeConflictError("committed batch identity mismatch")
        batch_session = str(getattr(batch, "session_id", ""))
        if batch_session != session_id:
            raise InputRuntimeConflictError(
                "committed batch belongs to another session"
            )
        source_event_ids = tuple(getattr(batch, "source_event_ids", ()) or ())
        if not source_event_ids:
            raise InputRuntimeConflictError(
                "committed batch provenance is incomplete"
            )
        if not str(getattr(batch, "content_fingerprint", "")).strip():
            raise InputRuntimeConflictError(
                "committed batch fingerprint is unavailable"
            )
        if getattr(batch, "committed_at", None) is None:
            raise InputRuntimeConflictError(
                "committed batch timestamp is unavailable"
            )
        return batch

    async def _get_or_create_state(
        self,
        session_id: str,
        *,
        now: datetime,
    ) -> SessionInputRuntimeState:
        state = await self.repositories.sessions.get(session_id)
        if state is not None:
            return state
        candidate = SessionInputRuntimeState(
            session_id=session_id,
            generation=0,
            created_at=now,
            updated_at=now,
        )
        try:
            return await self.repositories.sessions.create_if_absent(candidate)
        except InputRuntimeConflictError:
            current = await self.repositories.sessions.get(session_id)
            if current is None:
                raise
            return current

    @staticmethod
    def _decision(
        state: SessionInputRuntimeState,
    ) -> tuple[AdmissionKind, InputAdmissionAction, str]:
        if state.cycle_status in _TERMINAL_OR_IDLE:
            return (
                AdmissionKind.START_CYCLE,
                InputAdmissionAction.START_CYCLE,
                "input_runtime.admission.start_cycle",
            )
        try:
            kind, action = _ACTION_BY_STATUS[state.cycle_status]
        except KeyError as error:
            raise InputRuntimeConflictError(
                f"unsupported admission state: {state.cycle_status.value}"
            ) from error
        projection = {
            InputAdmissionAction.QUEUED_RUNNING:
                "input_runtime.admission.queued_running",
            InputAdmissionAction.RESUME_WAITING:
                "input_runtime.admission.resume_waiting",
            InputAdmissionAction.QUEUED_PAUSED:
                "input_runtime.admission.queued_paused",
            InputAdmissionAction.RESUME_INTERRUPTED:
                "input_runtime.admission.resume_interrupted",
        }[action]
        return kind, action, projection

    async def _capacity_reason(
        self,
        *,
        state: SessionInputRuntimeState,
        payload_size_bytes: int,
    ) -> str | None:
        if state.active_cycle_id is None:
            return None
        items = await self.repositories.inbox.list_for_cycle(
            state.active_cycle_id
        )
        pending = [item for item in items if item.state in _PENDING_INBOX_STATES]
        if len(pending) >= self.config.max_queued_batches_per_session:
            return "max_queued_batches_per_session"
        queued_bytes = sum(item.payload_size_bytes for item in pending)
        if (
            queued_bytes + payload_size_bytes
            > self.config.max_queued_bytes_per_session
        ):
            return "max_queued_bytes_per_session"
        return None

    async def _ensure_inbox(
        self,
        admission: InputAdmissionRecord,
        *,
        now: datetime,
    ) -> CycleInboxItem | None:
        if admission.cycle_sequence == 0:
            return None
        candidate = CycleInboxItem(
            admission_id=admission.admission_id,
            session_id=admission.session_id,
            cycle_id=admission.target_cycle_id,
            input_batch_id=admission.input_batch_id,
            cycle_sequence=admission.cycle_sequence,
            generation=admission.admitted_generation,
            payload_size_bytes=admission.payload_size_bytes,
            enqueued_at=now,
        )
        return await self.repositories.inbox.create_if_absent(candidate)

    @staticmethod
    def _flags(
        admission: InputAdmissionRecord,
        action: InputAdmissionAction,
    ) -> tuple[bool, bool]:
        if admission.state != AdmissionState.ADMITTED:
            return False, False
        if admission.admission_kind == AdmissionKind.START_CYCLE:
            return True, False
        if admission.admission_kind == AdmissionKind.QUEUE_PAUSED:
            return False, False
        return False, action != InputAdmissionAction.DUPLICATE

    async def _duplicate_outcome(
        self,
        admission: InputAdmissionRecord,
        *,
        requested_session_id: str,
        now: datetime,
    ) -> InputAdmissionOutcome:
        if admission.session_id != requested_session_id:
            raise InputRuntimeConflictError(
                "input batch is already admitted to another session"
            )
        await self._ensure_inbox(admission, now=now)
        should_start, should_wake = self._flags(
            admission,
            InputAdmissionAction.DUPLICATE,
        )
        if admission.admission_kind in {
            AdmissionKind.CONTINUE_RUNNING,
            AdmissionKind.RESUME_WAITING,
            AdmissionKind.RESUME_INTERRUPTED,
        } and admission.state == AdmissionState.ADMITTED:
            should_wake = True
        if should_wake:
            await self._wake_best_effort(admission)
        return InputAdmissionOutcome.accepted(
            admission=admission,
            action=InputAdmissionAction.DUPLICATE,
            should_start_runner=should_start,
            should_wake_runner=should_wake,
            user_projection_key="input_runtime.admission.duplicate",
            reason_code="existing_admission",
        )

    async def _wake_best_effort(self, admission: InputAdmissionRecord) -> None:
        try:
            await self.wake_coordinator.wake(
                admission.session_id,
                cycle_id=admission.target_cycle_id,
            )
        except Exception:
            # Durable inbox is authoritative; a lost in-process signal is safe
            # and will be reconciled by later checkpoint/recovery stages.
            return

    async def admit_committed_batch(
        self,
        input_batch_id: str,
        *,
        session_id: str,
    ) -> InputAdmissionOutcome:
        input_batch_id = input_batch_id.strip()
        session_id = session_id.strip()
        if not input_batch_id or not session_id:
            raise ValueError("input_batch_id and session_id are required")
        batch = await self._load_authoritative_batch(
            input_batch_id,
            session_id=session_id,
        )
        payload_size_bytes = int(self.payload_size_resolver(batch))
        if payload_size_bytes < 0:
            raise InputRuntimeConflictError(
                "committed batch payload size is invalid"
            )

        # One session-level application boundary makes capacity, sequence
        # allocation and inbox publication deterministic under concurrent
        # commits. Repository record-first recovery remains authoritative.
        async with self._hold_admission(session_id):
            now = self._now()
            existing = await self.repositories.admissions.get_by_input_batch_id(
                input_batch_id
            )
            if existing is not None:
                return await self._duplicate_outcome(
                    existing,
                    requested_session_id=session_id,
                    now=now,
                )

            state = await self._get_or_create_state(session_id, now=now)
            admission_kind, action, projection_key = self._decision(state)
            if admission_kind != AdmissionKind.START_CYCLE:
                capacity_reason = await self._capacity_reason(
                    state=state,
                    payload_size_bytes=payload_size_bytes,
                )
                if capacity_reason is not None:
                    return InputAdmissionOutcome(
                        input_batch_id=input_batch_id,
                        session_id=session_id,
                        action=InputAdmissionAction.CAPACITY_BLOCKED,
                        should_start_runner=False,
                        should_wake_runner=False,
                        user_projection_key=(
                            "input_runtime.admission.capacity_blocked"
                        ),
                        retryable=True,
                        reason_code=capacity_reason,
                    )

            target_cycle_id = (
                self.cycle_id_factory()
                if admission_kind == AdmissionKind.START_CYCLE
                else state.active_cycle_id
            )
            if not target_cycle_id:
                raise InputRuntimeConflictError(
                    "active cycle identity is unavailable"
                )
            candidate = InputAdmissionRecord(
                session_id=session_id,
                input_batch_id=input_batch_id,
                session_sequence=1,
                target_cycle_id=target_cycle_id,
                cycle_sequence=(
                    0 if admission_kind == AdmissionKind.START_CYCLE else 1
                ),
                admitted_generation=state.generation,
                payload_size_bytes=payload_size_bytes,
                admission_kind=admission_kind,
                idempotency_key=f"committed-input:{input_batch_id}",
                admitted_at=now,
            )
            admission = await self.repositories.admissions.allocate(candidate)
            if admission.session_id != session_id:
                raise InputRuntimeConflictError(
                    "allocated admission belongs to another session"
                )
            if admission.input_batch_id != input_batch_id:
                raise InputRuntimeConflictError(
                    "allocated admission input identity mismatch"
                )

            await self._ensure_inbox(admission, now=now)
            should_start = admission_kind == AdmissionKind.START_CYCLE
            should_wake = admission_kind not in {
                AdmissionKind.START_CYCLE,
                AdmissionKind.QUEUE_PAUSED,
            }
            if should_wake:
                await self._wake_best_effort(admission)
            return InputAdmissionOutcome.accepted(
                admission=admission,
                action=action,
                should_start_runner=should_start,
                should_wake_runner=should_wake,
                user_projection_key=projection_key,
                reason_code="admitted",
            )

    async def reconcile_committed_batch(
        self,
        input_batch_id: str,
        *,
        session_id: str,
    ) -> InputAdmissionOutcome:
        """IR-8 entry point: idempotently repair one committed batch relation."""
        return await self.admit_committed_batch(
            input_batch_id,
            session_id=session_id,
        )

    async def find_committed_without_admission(
        self,
        input_batch_ids: Iterable[str],
    ) -> tuple[Any, ...]:
        """Return discoverable committed candidates still lacking admission."""
        missing: list[Any] = []
        for input_batch_id in input_batch_ids:
            normalized = input_batch_id.strip()
            if not normalized:
                continue
            if (
                await self.repositories.admissions.get_by_input_batch_id(
                    normalized
                )
                is not None
            ):
                continue
            missing.append(
                await self.committed_batches.get_committed(normalized)
            )
        return tuple(missing)

    async def mark_initial_batch_applied(
        self,
        admission: InputAdmissionRecord,
    ) -> InputAdmissionRecord:
        if admission.admission_kind != AdmissionKind.START_CYCLE:
            raise InputRuntimeConflictError(
                "only initial admission can use initial apply marker"
            )
        current = await self.repositories.admissions.get_by_input_batch_id(
            admission.input_batch_id
        )
        if current is None:
            raise InputRuntimeConflictError("admission disappeared")
        if current.state == AdmissionState.APPLIED:
            return current
        return await self.repositories.admissions.mark_applied(
            current.admission_id,
            applied_at=self._now(),
        )

    async def begin_waiting_compatibility_apply(
        self,
        admission: InputAdmissionRecord,
    ) -> ClaimedInboxRange | None:
        """Temporarily claim WAITING_USER input for the legacy adapter."""
        if admission.admission_kind != AdmissionKind.RESUME_WAITING:
            raise InputRuntimeConflictError(
                "only WAITING_USER admission may use the IR-3 compatibility adapter"
            )
        if admission.state == AdmissionState.APPLIED:
            return None
        state = await self.repositories.sessions.get(admission.session_id)
        if state is None or state.active_cycle_id != admission.target_cycle_id:
            raise InputRuntimeConflictError(
                "compatibility apply lost active cycle authority"
            )
        claim = await self.repositories.inbox.claim_contiguous_range(
            admission.target_cycle_id,
            generation=admission.admitted_generation,
            after_sequence=state.active_cycle_applied_through_sequence,
            max_items=1,
            max_bytes=max(
                admission.payload_size_bytes,
                self.config.max_batch_bytes_per_checkpoint,
            ),
            lease_seconds=self.config.claim_lease_seconds,
        )
        if claim is None:
            return None
        if (
            len(claim.items) != 1
            or claim.items[0].admission_id != admission.admission_id
        ):
            await self.repositories.inbox.requeue_claim(
                claim,
                error_code="waiting_compatibility_order_conflict",
            )
            raise InputRuntimeConflictError(
                "compatibility apply must claim the exact FIFO head"
            )
        return await self.repositories.inbox.mark_applying(claim)

    async def complete_waiting_compatibility_apply(
        self,
        claim: ClaimedInboxRange,
    ) -> None:
        """IR-3 compatibility ownership; replace with common IR-4 applier."""
        applied_at = self._now()
        items = await self.repositories.inbox.mark_applied(
            claim,
            applied_at=applied_at,
        )
        for item in items:
            await self.repositories.admissions.mark_applied(
                item.admission_id,
                applied_at=applied_at,
            )
        await self._advance_applied_watermark(
            session_id=items[0].session_id,
            cycle_id=items[0].cycle_id,
            generation=items[0].generation,
            sequence=items[-1].cycle_sequence,
            now=applied_at,
        )

    async def requeue_waiting_compatibility_apply(
        self,
        claim: ClaimedInboxRange,
        *,
        error_code: str,
    ) -> None:
        await self.repositories.inbox.requeue_claim(
            claim,
            error_code=error_code,
        )

    async def _advance_applied_watermark(
        self,
        *,
        session_id: str,
        cycle_id: str,
        generation: int,
        sequence: int,
        now: datetime,
    ) -> SessionInputRuntimeState:
        for _ in range(8):
            state = await self.repositories.sessions.get(session_id)
            if state is None:
                raise InputRuntimeConflictError("session state disappeared")
            if (
                state.generation != generation
                or state.active_cycle_id != cycle_id
            ):
                raise InputRuntimeConflictError(
                    "stale compatibility apply authority"
                )
            if state.active_cycle_applied_through_sequence >= sequence:
                return state
            updated = state.model_copy(
                update={
                    "active_cycle_applied_through_sequence": sequence,
                    "revision": state.revision + 1,
                    "updated_at": max(state.updated_at, now),
                }
            )
            updated = SessionInputRuntimeState.model_validate(
                updated.model_dump(mode="python")
            )
            try:
                return await self.repositories.sessions.compare_and_swap(
                    state.revision,
                    updated,
                )
            except InputRuntimeConflictError:
                continue
        raise InputRuntimeConflictError(
            "failed to advance applied watermark after retries"
        )

    async def record_cycle_status(
        self,
        *,
        session_id: str,
        cycle_id: str,
        status: CycleStatus,
    ) -> SessionInputRuntimeState:
        now = self._now()
        for _ in range(8):
            state = await self.repositories.sessions.get(session_id)
            if state is None:
                raise InputRuntimeConflictError("session state disappeared")
            if state.active_cycle_id != cycle_id:
                raise InputRuntimeConflictError(
                    "cycle status update lost active-cycle authority"
                )
            effective = status
            if (
                status in {
                    CycleStatus.DONE,
                    CycleStatus.ERROR,
                    CycleStatus.CANCELLED,
                }
                and state.active_cycle_applied_through_sequence
                < state.active_cycle_accepted_through_sequence
            ):
                effective = CycleStatus.INTERRUPTED
            if state.cycle_status == effective:
                return state
            updated = state.model_copy(
                update={
                    "cycle_status": effective,
                    "finalization_id": None,
                    "revision": state.revision + 1,
                    "updated_at": max(state.updated_at, now),
                }
            )
            updated = SessionInputRuntimeState.model_validate(
                updated.model_dump(mode="python")
            )
            try:
                return await self.repositories.sessions.compare_and_swap(
                    state.revision,
                    updated,
                )
            except InputRuntimeConflictError:
                continue
        raise InputRuntimeConflictError(
            "failed to record cycle status after retries"
        )
