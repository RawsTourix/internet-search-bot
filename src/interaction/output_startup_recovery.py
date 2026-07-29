"""Safe startup reconciliation for READY OutputBatch authority."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .output_models import OutputBatchState
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
    remaining_ready: tuple[ReadyOutputAuthority, ...]

    @property
    def cancelled_count(self) -> int:
        return len(self.cancelled_legacy_output_batch_ids)


async def reconcile_unclaimable_legacy_ready(
    store: FileSystemOutputBatchStore,
) -> OutputStartupRecoveryReport:
    """Cancel only READY batches carrying the explicit legacy sentinel.

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
        remaining_ready=remaining,
    )
    logger.info(
        "output_startup_authority_reconciliation_completed "
        "cancelled_legacy=%s remaining_ready=%s",
        report.cancelled_count,
        len(report.remaining_ready),
    )
    return report


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
