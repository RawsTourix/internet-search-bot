"""FIFO application of committed additions to one active agent cycle."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from ..memory import validate_openai_tool_sequence
from .config import InputRuntimeConfigType
from .errors import InputRuntimeConflictError
from .factory import InputRuntimeRepositories
from .handoff import RuntimeHandoffState
from .models import (
    ActiveCycleSnapshot,
    AdmissionKind,
    AdmissionState,
    CheckpointAction,
    CheckpointName,
    CheckpointOutcome,
    ClaimedInboxRange,
    CycleContextRevision,
    CycleStatus,
    InboxState,
    SessionInputRuntimeState,
    new_context_revision_id,
)
from .projection import build_input_batch_update, build_input_batch_update_message


Clock = Callable[[], datetime]
RevisionIdFactory = Callable[[], str]


class CycleInputApplier:
    """Apply bounded contiguous inbox ranges using snapshot-first authority."""

    def __init__(
        self,
        *,
        config: InputRuntimeConfigType,
        repositories: InputRuntimeRepositories,
        committed_batches: Any,
        clock: Clock | None = None,
        revision_id_factory: RevisionIdFactory | None = None,
    ) -> None:
        self.config = config
        self.repositories = repositories
        self.committed_batches = committed_batches
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.revision_id_factory = revision_id_factory or new_context_revision_id

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("runtime clock must return timezone-aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _status(value: Any) -> CycleStatus:
        raw = str(getattr(value, "value", value) or "running")
        try:
            return CycleStatus(raw)
        except ValueError:
            return CycleStatus.RUNNING

    @staticmethod
    def _active_messages(active_cycle: Any) -> list[dict[str, Any]]:
        return [dict(item) for item in active_cycle.messages_for_llm]

    @staticmethod
    def _active_artifact_refs(active_cycle: Any) -> list[str]:
        return list(dict.fromkeys(getattr(active_cycle, "artifact_refs", ()) or ()))

    def _snapshot_from_active(
        self,
        *,
        active_cycle: Any,
        generation: int,
        initial_batch_id: str,
        revision: CycleContextRevision,
        checkpoint: CheckpointName,
        now: datetime,
        snapshot_revision: int = 1,
        applied_batch_ids: list[str] | None = None,
        applied_through: int = 0,
        messages: list[dict[str, Any]] | None = None,
        artifact_refs: list[str] | None = None,
        status: CycleStatus | None = None,
        interruption_reason: str | None = None,
    ) -> ActiveCycleSnapshot:
        effective_status = status or self._status(getattr(active_cycle, "status", None))
        if effective_status == CycleStatus.WAITING_USER and not getattr(
            active_cycle, "waiting_question", None
        ):
            effective_status = CycleStatus.RUNNING
        if effective_status == CycleStatus.INTERRUPTED and not interruption_reason:
            interruption_reason = (
                getattr(active_cycle, "interruption_reason", None)
                or "input_runtime_checkpoint_interrupted"
            )
        return ActiveCycleSnapshot(
            cycle_id=str(active_cycle.cycle_id),
            session_id=str(active_cycle.session_id),
            generation=generation,
            status=effective_status,
            original_input_batch_id=initial_batch_id,
            original_user_request=str(active_cycle.original_user_request),
            messages_for_llm=messages or self._active_messages(active_cycle),
            cycle_trace=[dict(item) for item in active_cycle.cycle_trace],
            working_memory_ref=None,
            applied_input_batch_ids=list(applied_batch_ids or [initial_batch_id]),
            applied_through_cycle_sequence=applied_through,
            active_context_revision_id=revision.context_revision_id,
            waiting_question=getattr(active_cycle, "waiting_question", None),
            interruption_reason=interruption_reason,
            active_plan_id=getattr(active_cycle, "active_plan_id", None),
            active_plan_revision=getattr(active_cycle, "active_plan_revision", None),
            active_plan_node_id=getattr(active_cycle, "active_plan_node_id", None),
            artifact_refs=list(artifact_refs or self._active_artifact_refs(active_cycle)),
            read_artifact_refs=list(
                dict.fromkeys(getattr(active_cycle, "read_artifact_refs", ()) or ())
            ),
            result_refs=list(dict.fromkeys(getattr(active_cycle, "result_refs", ()) or ())),
            snapshot_revision=snapshot_revision,
            safe_checkpoint=checkpoint,
            created_at=now,
            updated_at=now,
        )

    async def _advance_session_authority(
        self,
        *,
        state: SessionInputRuntimeState,
        context_revision_id: str,
        applied_through: int,
        now: datetime,
    ) -> SessionInputRuntimeState:
        current = state
        for _ in range(8):
            if (
                current.active_cycle_applied_through_sequence >= applied_through
                and current.active_context_revision_id == context_revision_id
            ):
                return current
            updated = current.model_copy(
                update={
                    "active_cycle_applied_through_sequence": max(
                        current.active_cycle_applied_through_sequence,
                        applied_through,
                    ),
                    "active_context_revision_id": context_revision_id,
                    "revision": current.revision + 1,
                    "updated_at": max(current.updated_at, now),
                }
            )
            updated = SessionInputRuntimeState.model_validate(
                updated.model_dump(mode="python")
            )
            try:
                return await self.repositories.sessions.compare_and_swap(
                    current.revision, updated
                )
            except InputRuntimeConflictError:
                refreshed = await self.repositories.sessions.get(current.session_id)
                if refreshed is None:
                    raise InputRuntimeConflictError("session state disappeared")
                if (
                    refreshed.generation != current.generation
                    or refreshed.active_cycle_id != current.active_cycle_id
                ):
                    raise InputRuntimeConflictError("stale cycle authority")
                current = refreshed
        raise InputRuntimeConflictError("failed to advance session authority")

    @staticmethod
    def _install_snapshot(active_cycle: Any, snapshot: ActiveCycleSnapshot) -> None:
        validate_openai_tool_sequence(snapshot.messages_for_llm)
        active_cycle.messages_for_llm[:] = [
            dict(item) for item in snapshot.messages_for_llm
        ]
        active_cycle.artifact_refs[:] = list(snapshot.artifact_refs)
        if hasattr(active_cycle, "read_artifact_refs"):
            active_cycle.read_artifact_refs[:] = list(snapshot.read_artifact_refs)
        if hasattr(active_cycle, "result_refs"):
            active_cycle.result_refs[:] = list(snapshot.result_refs)
        active_cycle.original_input_batch_id = snapshot.original_input_batch_id
        active_cycle.active_plan_id = snapshot.active_plan_id
        active_cycle.active_plan_revision = snapshot.active_plan_revision
        active_cycle.active_plan_node_id = snapshot.active_plan_node_id
        active_cycle.status = snapshot.status.value
        active_cycle.waiting_question = snapshot.waiting_question
        active_cycle.interruption_reason = snapshot.interruption_reason
        active_cycle.input_runtime_generation = snapshot.generation
        active_cycle.active_context_revision_id = snapshot.active_context_revision_id
        active_cycle.applied_input_batch_ids = list(snapshot.applied_input_batch_ids)
        active_cycle.applied_through_cycle_sequence = (
            snapshot.applied_through_cycle_sequence
        )
        active_cycle.input_runtime_safe_checkpoint = snapshot.safe_checkpoint.value
        active_cycle.input_runtime_snapshot_revision = snapshot.snapshot_revision

    async def _mark_snapshot_applied_records(
        self,
        snapshot: ActiveCycleSnapshot,
        *,
        now: datetime,
    ) -> None:
        admissions = await self.repositories.admissions.list_for_session(
            snapshot.session_id
        )
        by_batch = {item.input_batch_id: item for item in admissions}
        for batch_id in snapshot.applied_input_batch_ids:
            admission = by_batch.get(batch_id)
            if admission is not None and admission.state == AdmissionState.ADMITTED:
                await self.repositories.admissions.mark_applied(
                    admission.admission_id, applied_at=now
                )

        items = await self.repositories.inbox.list_for_cycle(snapshot.cycle_id)
        groups: dict[str, list[Any]] = defaultdict(list)
        for item in items:
            if (
                item.cycle_sequence <= snapshot.applied_through_cycle_sequence
                and item.state in {InboxState.CLAIMED, InboxState.APPLYING}
                and item.claim_token
            ):
                groups[item.claim_token].append(item)
        for token, group in groups.items():
            group.sort(key=lambda item: item.cycle_sequence)
            claim = ClaimedInboxRange(
                cycle_id=snapshot.cycle_id,
                generation=snapshot.generation,
                claim_token=token,
                first_cycle_sequence=group[0].cycle_sequence,
                last_cycle_sequence=group[-1].cycle_sequence,
                items=tuple(group),
                claimed_bytes=sum(item.payload_size_bytes for item in group),
                claim_expires_at=group[0].claim_expires_at,
            )
            await self.repositories.inbox.mark_applied(claim, applied_at=now)

    async def ensure_initial_context(
        self,
        *,
        session_id: str,
        cycle_id: str,
        generation: int,
        checkpoint: CheckpointName,
        active_cycle: Any,
        input_batch_id: str,
    ) -> CheckpointOutcome:
        now = self._now()
        state = await self.repositories.sessions.get(session_id)
        if (
            state is None
            or state.active_cycle_id != cycle_id
            or state.generation != generation
        ):
            return CheckpointOutcome(
                checkpoint=checkpoint,
                action=CheckpointAction.INTERRUPT,
                applied_through_cycle_sequence=0,
                reason_code="stale_initial_cycle_authority",
            )
        admission = await self.repositories.admissions.get_by_input_batch_id(
            input_batch_id
        )
        if (
            admission is None
            or admission.admission_kind != AdmissionKind.START_CYCLE
            or admission.target_cycle_id != cycle_id
            or admission.admitted_generation != generation
        ):
            return CheckpointOutcome(
                checkpoint=checkpoint,
                action=CheckpointAction.INTERRUPT,
                reason_code="initial_admission_mismatch",
            )

        snapshot = await self.repositories.snapshots.get(cycle_id)
        if snapshot is not None:
            if snapshot.generation != generation:
                return CheckpointOutcome(
                    checkpoint=checkpoint,
                    action=CheckpointAction.INTERRUPT,
                    reason_code="stale_snapshot_generation",
                )
            await self._mark_snapshot_applied_records(snapshot, now=now)
            state = await self._advance_session_authority(
                state=state,
                context_revision_id=snapshot.active_context_revision_id,
                applied_through=snapshot.applied_through_cycle_sequence,
                now=now,
            )
            self._install_snapshot(active_cycle, snapshot)
            return CheckpointOutcome(
                checkpoint=checkpoint,
                action=CheckpointAction.CONTINUE,
                context_revision_id=snapshot.active_context_revision_id,
                applied_through_cycle_sequence=(
                    snapshot.applied_through_cycle_sequence
                ),
            )

        latest = await self.repositories.context_revisions.get_latest(cycle_id)
        if latest is None:
            revision = CycleContextRevision(
                context_revision_id=self.revision_id_factory(),
                cycle_id=cycle_id,
                session_id=session_id,
                revision_number=1,
                reason="initial_input",
                applied_input_batch_ids=[input_batch_id],
                applied_through_cycle_sequence=0,
                added_artifact_refs=list(
                    dict.fromkeys(getattr(active_cycle, "artifact_refs", ()) or ())
                ),
                created_at=now,
            )
            revision = await self.repositories.context_revisions.append_revision(
                revision
            )
        else:
            if (
                latest.revision_number != 1
                or latest.reason != "initial_input"
                or latest.applied_input_batch_ids != [input_batch_id]
            ):
                return CheckpointOutcome(
                    checkpoint=checkpoint,
                    action=CheckpointAction.INTERRUPT,
                    reason_code="divergent_initial_context_revision",
                )
            revision = latest

        active_cycle.original_input_batch_id = input_batch_id
        initial_refs = list(
            dict.fromkeys(getattr(active_cycle, "artifact_refs", ()) or ())
        )
        candidate = self._snapshot_from_active(
            active_cycle=active_cycle,
            generation=generation,
            initial_batch_id=input_batch_id,
            revision=revision,
            checkpoint=checkpoint,
            now=now,
            applied_batch_ids=[input_batch_id],
            applied_through=0,
            artifact_refs=initial_refs,
            status=CycleStatus.RUNNING,
        )
        snapshot = await self.repositories.snapshots.create_if_absent(candidate)
        await self._mark_snapshot_applied_records(snapshot, now=now)
        await self._advance_session_authority(
            state=state,
            context_revision_id=revision.context_revision_id,
            applied_through=0,
            now=now,
        )
        self._install_snapshot(active_cycle, snapshot)
        return CheckpointOutcome(
            checkpoint=checkpoint,
            action=CheckpointAction.CONTINUE,
            context_revision_id=revision.context_revision_id,
            applied_through_cycle_sequence=0,
        )

    async def _unsafe_handoff_reason(
        self,
        *,
        cycle_id: str,
        after_sequence: int,
    ) -> str | None:
        items = await self.repositories.inbox.list_for_cycle(cycle_id)
        for item in items:
            if item.cycle_sequence <= after_sequence:
                continue
            if item.state != InboxState.APPLYING:
                continue
            marker = await self.repositories.handoffs.get(item.admission_id)
            if marker is not None and marker.state in {
                RuntimeHandoffState.HANDED_OFF,
                RuntimeHandoffState.AMBIGUOUS,
            }:
                return "ambiguous_runtime_handoff_requires_recovery"
        return None

    async def apply_pending_input(
        self,
        *,
        session_id: str,
        cycle_id: str,
        generation: int,
        checkpoint: CheckpointName,
        active_cycle: Any,
    ) -> CheckpointOutcome:
        now = self._now()
        state = await self.repositories.sessions.get(session_id)
        snapshot = await self.repositories.snapshots.get(cycle_id)
        if (
            state is None
            or snapshot is None
            or state.active_cycle_id != cycle_id
            or state.generation != generation
            or snapshot.session_id != session_id
            or snapshot.generation != generation
        ):
            return CheckpointOutcome(
                checkpoint=checkpoint,
                action=CheckpointAction.INTERRUPT,
                reason_code="checkpoint_cycle_authority_mismatch",
            )

        await self._mark_snapshot_applied_records(snapshot, now=now)
        state = await self._advance_session_authority(
            state=state,
            context_revision_id=snapshot.active_context_revision_id,
            applied_through=snapshot.applied_through_cycle_sequence,
            now=now,
        )
        self._install_snapshot(active_cycle, snapshot)

        unsafe = await self._unsafe_handoff_reason(
            cycle_id=cycle_id,
            after_sequence=snapshot.applied_through_cycle_sequence,
        )
        if unsafe:
            return CheckpointOutcome(
                checkpoint=checkpoint,
                action=CheckpointAction.INTERRUPT,
                context_revision_id=snapshot.active_context_revision_id,
                applied_through_cycle_sequence=(
                    snapshot.applied_through_cycle_sequence
                ),
                reason_code=unsafe,
            )

        claim = await self.repositories.inbox.claim_contiguous_range(
            cycle_id,
            generation=generation,
            after_sequence=snapshot.applied_through_cycle_sequence,
            max_items=self.config.max_batches_per_checkpoint,
            max_bytes=self.config.max_batch_bytes_per_checkpoint,
            lease_seconds=self.config.claim_lease_seconds,
        )
        if claim is None:
            pending = [
                item
                for item in await self.repositories.inbox.list_for_cycle(cycle_id)
                if item.cycle_sequence > snapshot.applied_through_cycle_sequence
                and item.state == InboxState.QUEUED
            ]
            pending.sort(key=lambda item: item.cycle_sequence)
            if (
                pending
                and pending[0].payload_size_bytes
                > self.config.max_batch_bytes_per_checkpoint
            ):
                return CheckpointOutcome(
                    checkpoint=checkpoint,
                    action=CheckpointAction.INTERRUPT,
                    context_revision_id=snapshot.active_context_revision_id,
                    applied_through_cycle_sequence=(
                        snapshot.applied_through_cycle_sequence
                    ),
                    reason_code="checkpoint_head_batch_too_large",
                )
            return CheckpointOutcome(
                checkpoint=checkpoint,
                action=CheckpointAction.CONTINUE,
                context_revision_id=snapshot.active_context_revision_id,
                applied_through_cycle_sequence=(
                    snapshot.applied_through_cycle_sequence
                ),
            )

        claim = await self.repositories.inbox.mark_applying(claim)
        try:
            expected = list(
                range(
                    snapshot.applied_through_cycle_sequence + 1,
                    claim.last_cycle_sequence + 1,
                )
            )
            if [item.cycle_sequence for item in claim.items] != expected:
                raise InputRuntimeConflictError("claimed input range has a gap")

            loaded: list[tuple[Any, int]] = []
            applied_ids: list[str] = []
            added_refs: list[str] = []
            for item in claim.items:
                admission = await self.repositories.admissions.get_by_input_batch_id(
                    item.input_batch_id
                )
                if (
                    admission is None
                    or admission.admission_id != item.admission_id
                    or admission.target_cycle_id != cycle_id
                    or admission.admitted_generation != generation
                    or admission.cycle_sequence != item.cycle_sequence
                ):
                    raise InputRuntimeConflictError("inbox/admission relation mismatch")
                marker = await self.repositories.handoffs.get(item.admission_id)
                if marker is not None and marker.state == RuntimeHandoffState.AMBIGUOUS:
                    raise InputRuntimeConflictError(
                        "ambiguous runtime handoff cannot be replayed"
                    )
                batch = await self.committed_batches.get_committed(item.input_batch_id)
                if (
                    str(getattr(batch, "input_batch_id", "")) != item.input_batch_id
                    or str(getattr(batch, "session_id", "")) != session_id
                ):
                    raise InputRuntimeConflictError("committed batch identity mismatch")
                loaded.append((batch, item.cycle_sequence))
                applied_ids.append(item.input_batch_id)
                added_refs.extend(list(getattr(batch, "artifact_refs", ()) or ()))

            latest = await self.repositories.context_revisions.get_latest(cycle_id)
            if latest is None:
                raise InputRuntimeConflictError("initial context revision is missing")
            if latest.context_revision_id == snapshot.active_context_revision_id:
                revision = CycleContextRevision(
                    context_revision_id=self.revision_id_factory(),
                    cycle_id=cycle_id,
                    session_id=session_id,
                    revision_number=latest.revision_number + 1,
                    parent_revision_ids=[latest.context_revision_id],
                    reason="input_applied",
                    applied_input_batch_ids=applied_ids,
                    applied_through_cycle_sequence=claim.last_cycle_sequence,
                    added_artifact_refs=list(dict.fromkeys(added_refs)),
                    constraint_summary=f"checkpoint:{checkpoint.value}",
                    created_at=now,
                )
                revision = await self.repositories.context_revisions.append_revision(
                    revision
                )
            elif (
                latest.parent_revision_ids == [snapshot.active_context_revision_id]
                and latest.reason == "input_applied"
                and latest.applied_input_batch_ids == applied_ids
                and latest.applied_through_cycle_sequence
                == claim.last_cycle_sequence
            ):
                revision = latest
            else:
                raise InputRuntimeConflictError("orphan divergent revision detected")

            base_messages = [dict(item) for item in snapshot.messages_for_llm]
            validate_openai_tool_sequence(base_messages)
            payload = build_input_batch_update(
                context_revision_id=revision.context_revision_id,
                batches=loaded,
            )
            candidate_messages = [
                *base_messages,
                build_input_batch_update_message(payload),
            ]
            validate_openai_tool_sequence(candidate_messages)
            candidate_refs = list(
                dict.fromkeys([*snapshot.artifact_refs, *added_refs])
            )
            candidate_snapshot = self._snapshot_from_active(
                active_cycle=active_cycle,
                generation=generation,
                initial_batch_id=snapshot.original_input_batch_id,
                revision=revision,
                checkpoint=checkpoint,
                now=now,
                snapshot_revision=snapshot.snapshot_revision + 1,
                applied_batch_ids=list(
                    dict.fromkeys([
                        *snapshot.applied_input_batch_ids,
                        *applied_ids,
                    ])
                ),
                applied_through=claim.last_cycle_sequence,
                messages=candidate_messages,
                artifact_refs=candidate_refs,
                status=CycleStatus.RUNNING,
            )
            persisted = await self.repositories.snapshots.compare_and_swap(
                snapshot.snapshot_revision, candidate_snapshot
            )
            try:
                self._install_snapshot(active_cycle, persisted)
            except Exception:
                interrupted = persisted.model_copy(
                    update={
                        "status": CycleStatus.INTERRUPTED,
                        "interruption_reason": (
                            "candidate_context_install_failed"
                        ),
                        "snapshot_revision": persisted.snapshot_revision + 1,
                        "safe_checkpoint": CheckpointName.AFTER_INTERRUPTION,
                        "updated_at": self._now(),
                    }
                )
                interrupted = ActiveCycleSnapshot.model_validate(
                    interrupted.model_dump(mode="python")
                )
                await self.repositories.snapshots.compare_and_swap(
                    persisted.snapshot_revision, interrupted
                )
                return CheckpointOutcome(
                    checkpoint=checkpoint,
                    action=CheckpointAction.INTERRUPT,
                    context_revision_id=revision.context_revision_id,
                    applied_through_cycle_sequence=claim.last_cycle_sequence,
                    reason_code="candidate_context_install_failed",
                )

            applied_at = self._now()
            items = await self.repositories.inbox.mark_applied(
                claim, applied_at=applied_at
            )
            for item in items:
                admission = await self.repositories.admissions.get_by_input_batch_id(
                    item.input_batch_id
                )
                if admission is not None and admission.state == AdmissionState.ADMITTED:
                    await self.repositories.admissions.mark_applied(
                        admission.admission_id, applied_at=applied_at
                    )
            await self._advance_session_authority(
                state=state,
                context_revision_id=revision.context_revision_id,
                applied_through=claim.last_cycle_sequence,
                now=applied_at,
            )
            return CheckpointOutcome(
                checkpoint=checkpoint,
                action=CheckpointAction.INPUT_APPLIED,
                context_revision_id=revision.context_revision_id,
                applied_through_cycle_sequence=claim.last_cycle_sequence,
                applied_input_batch_ids=tuple(applied_ids),
            )
        except Exception:
            current = await self.repositories.snapshots.get(cycle_id)
            if (
                current is None
                or current.applied_through_cycle_sequence
                < claim.last_cycle_sequence
            ):
                await self.repositories.inbox.requeue_claim(
                    claim, error_code="checkpoint_apply_failed"
                )
            raise
