"""Safe startup reconciliation for READY OutputBatch authority."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from ..artifacts.delivery import FileSystemArtifactDeliveryStore
from ..artifacts.errors import (
    ArtifactDeliveryError,
    ArtifactDeliveryNotFoundError,
    ArtifactIntegrityError,
    ArtifactStorageError,
)
from ..artifacts.models import ArtifactDeliveryState
from .output_models import ArtifactOutputPart, OutputBatch, OutputBatchState
from .output_store import FileSystemOutputBatchStore


logger = logging.getLogger("Interaction.OutputStartupRecovery")
_LEGACY_INSTANCE_PREFIX = "legacy-committed-batch:"


@dataclass(frozen=True, slots=True)
class ReadyOutputAuthority:
    output_batch_id: str
    session_id: str
    client_type: str
    client_instance_id: str
    kind: str


@dataclass(frozen=True, slots=True)
class OutputStartupRecoveryReport:
    cancelled_legacy_output_batch_ids: tuple[str, ...]
    repaired_output_batch_ids: tuple[str, ...]
    unrepaired_output_batch_ids: tuple[str, ...]
    remaining_ready: tuple[ReadyOutputAuthority, ...]

    @property
    def cancelled_count(self) -> int:
        return len(self.cancelled_legacy_output_batch_ids)

    @property
    def repaired_count(self) -> int:
        return len(self.repaired_output_batch_ids)


async def reconcile_unclaimable_legacy_ready(
    store: FileSystemOutputBatchStore,
    delivery_store: FileSystemArtifactDeliveryStore | None = None,
) -> OutputStartupRecoveryReport:
    """Cancel legacy authority and repair safe READY artifact ownership.

    Exact outbox authority requires a real ``client_instance_id``. Historical
    compatibility batches used ``legacy-committed-batch:<client>`` instead;
    no transport worker can legally claim them. They are retained as terminal
    audit records rather than left in READY forever.
    """

    recoverable = await store.list_recoverable()
    targets = [
        batch
        for batch in recoverable
        if batch.state == OutputBatchState.READY
        and batch.capability_snapshot.client_instance_id.startswith(
            _LEGACY_INSTANCE_PREFIX
        )
    ]
    cancelled: list[str] = []
    for batch in targets:
        changed = await asyncio.to_thread(
            _cancel_ready_sync,
            store,
            batch.output_batch_id,
        )
        if changed:
            cancelled.append(batch.output_batch_id)

    repaired: list[str] = []
    unrepaired: list[str] = []
    if delivery_store is not None:
        for batch in recoverable:
            if (
                batch.state != OutputBatchState.READY
                or batch.output_batch_id in cancelled
                or batch.capability_snapshot.client_instance_id.startswith(
                    _LEGACY_INSTANCE_PREFIX
                )
            ):
                continue
            try:
                changed = await _repair_ready_artifact_ownership(
                    batch,
                    delivery_store,
                )
            except (
                ArtifactDeliveryError,
                ArtifactDeliveryNotFoundError,
                ArtifactIntegrityError,
                ArtifactStorageError,
            ) as error:
                unrepaired.append(batch.output_batch_id)
                logger.error(
                    "output_startup_ownership_repair_rejected "
                    "output_batch_id=%s error_type=%s error=%s",
                    batch.output_batch_id,
                    type(error).__name__,
                    error,
                )
            else:
                if changed:
                    repaired.append(batch.output_batch_id)

    remaining_batches = await store.list_recoverable()
    remaining = tuple(
        ReadyOutputAuthority(
            output_batch_id=batch.output_batch_id,
            session_id=batch.session_id,
            client_type=batch.capability_snapshot.client_type,
            client_instance_id=(
                batch.capability_snapshot.client_instance_id
            ),
            kind=batch.kind.value,
        )
        for batch in remaining_batches
        if batch.state == OutputBatchState.READY
    )
    report = OutputStartupRecoveryReport(
        cancelled_legacy_output_batch_ids=tuple(cancelled),
        repaired_output_batch_ids=tuple(repaired),
        unrepaired_output_batch_ids=tuple(unrepaired),
        remaining_ready=remaining,
    )
    logger.info(
        "output_startup_authority_reconciliation_completed "
        "cancelled_legacy=%s repaired_ownership=%s unrepaired=%s "
        "remaining_ready=%s",
        report.cancelled_count,
        report.repaired_count,
        len(report.unrepaired_output_batch_ids),
        len(report.remaining_ready),
    )
    return report


async def _repair_ready_artifact_ownership(
    batch: OutputBatch,
    delivery_store: FileSystemArtifactDeliveryStore,
) -> bool:
    parts = [
        part for part in batch.parts if isinstance(part, ArtifactOutputPart)
    ]
    if not parts:
        return False
    records = [
        await delivery_store.get(part.delivery_id) for part in parts
    ]
    existing_input_ids = {
        record.input_batch_id
        for record in records
        if record.input_batch_id is not None
    }
    if batch.input_batch_id is None:
        if len(existing_input_ids) > 1:
            raise ArtifactDeliveryError(
                "READY OutputBatch deliveries disagree on InputBatch authority"
            )
        repair_input_batch_id = next(iter(existing_input_ids), None)
    else:
        repair_input_batch_id = batch.input_batch_id
    for part, record in zip(parts, records, strict=True):
        if (
            record.session_id != batch.session_id
            or record.cycle_id != batch.cycle_id
            or record.client_type != batch.capability_snapshot.client_type
            or record.artifact_id != part.artifact_id
            or record.filename != part.filename
            or record.mime_type != part.mime_type
            or record.size_bytes != part.size_bytes
            or record.state
            not in {
                ArtifactDeliveryState.SELECTED,
                ArtifactDeliveryState.FAILED,
            }
        ):
            raise ArtifactDeliveryError(
                "READY OutputBatch artifact is unsafe for ownership repair"
            )
        if record.output_batch_id not in {None, batch.output_batch_id}:
            raise ArtifactDeliveryError(
                "Artifact delivery belongs to another OutputBatch"
            )
        if record.client_instance_id not in {
            None,
            batch.capability_snapshot.client_instance_id,
        }:
            raise ArtifactDeliveryError(
                "Artifact delivery belongs to another client instance"
            )
        if record.input_batch_id not in {None, repair_input_batch_id}:
            raise ArtifactDeliveryError(
                "Artifact delivery belongs to another InputBatch"
            )

    changed = any(
        record.output_batch_id != batch.output_batch_id
        or record.client_instance_id
        != batch.capability_snapshot.client_instance_id
        or (
            repair_input_batch_id is not None
            and record.input_batch_id != repair_input_batch_id
        )
        for record in records
    )
    if not changed:
        return False
    bound = await delivery_store.bind_output_batch(
        [part.delivery_id for part in parts],
        output_batch_id=batch.output_batch_id,
        input_batch_id=repair_input_batch_id,
        client_instance_id=batch.capability_snapshot.client_instance_id,
    )
    trace = getattr(delivery_store, "trace_output_bindings", None)
    if trace is not None:
        await trace(bound)
    logger.warning(
        "output_startup_ownership_repaired output_batch_id=%s "
        "input_batch_id=%s delivery_count=%s",
        batch.output_batch_id,
        repair_input_batch_id,
        len(bound),
    )
    return True


def _cancel_ready_sync(
    store: FileSystemOutputBatchStore,
    output_batch_id: str,
) -> bool:
    now = datetime.now(timezone.utc)
    with store._lock:
        current = store._load_sync(output_batch_id)
        if current.state != OutputBatchState.READY:
            return False
        instance_id = current.capability_snapshot.client_instance_id
        if not instance_id.startswith(_LEGACY_INSTANCE_PREFIX):
            return False
        state_path = store.records / output_batch_id / "state.json"
        state = store._read(state_path)
        state.update(
            state=OutputBatchState.CANCELLED.value,
            completed_at=now.isoformat(),
            updated_at=now.isoformat(),
            error_code="unclaimable_legacy_client_instance",
        )
        store._write(state_path, state)
        logger.warning(
            "output_batch_cancelled_unclaimable_legacy "
            "output_batch_id=%s session_id=%s client_type=%s "
            "client_instance_id=%s",
            output_batch_id,
            current.session_id,
            current.capability_snapshot.client_type,
            instance_id,
        )
        return True
