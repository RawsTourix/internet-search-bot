"""IR-7 OutputBatch rollback for a superseded unclaimed final candidate.

This module is deliberately infrastructure-facing: it coordinates the existing
filesystem OutputBatch and artifact-delivery stores.  Finalization application
logic only asks the composed assembler to abandon a stale READY aggregate.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..artifacts.delivery import ArtifactDeliveryRecord
from .errors import OutputBatchNotFoundError
from .output_models import ArtifactOutputPart, OutputBatchState


async def abandon_uncommitted_final_output(
    assembler: Any,
    *,
    output_batch_id: str,
) -> None:
    """Remove one unclaimed stale aggregate and release artifact ownership.

    The operation is replay-safe.  A finalization abort is durable authority;
    this cleanup only frees the cycle/kind commit-once identity so the same
    cycle can later produce a new final answer after applying late input.
    """

    await asyncio.to_thread(
        _abandon_sync,
        assembler,
        output_batch_id,
    )


def _abandon_sync(assembler: Any, output_batch_id: str) -> None:
    output_store = assembler.output_store
    delivery_store = assembler.delivery_store
    with output_store._lock, delivery_store._lock:
        try:
            batch = output_store._load_sync(output_batch_id)
        except OutputBatchNotFoundError:
            batch = None

        delivery_ids: list[str] = []
        if batch is not None:
            if batch.state != OutputBatchState.READY:
                raise RuntimeError(
                    "IR-7 cannot abandon an OutputBatch after delivery claim"
                )
            delivery_ids = [
                part.delivery_id
                for part in batch.parts
                if isinstance(part, ArtifactOutputPart)
            ]
        else:
            # A prior retry may already have rolled back the aggregate.  Find
            # only exact bindings to this output ID; no cycle-wide mutation.
            for path in sorted(delivery_store.root.glob("dlv_*.json")):
                record = delivery_store._load_path_sync(path)
                if record.output_batch_id == output_batch_id:
                    delivery_ids.append(record.delivery_id)

        updates: dict[str, tuple[ArtifactDeliveryRecord, bool]] = {}
        for delivery_id in delivery_ids:
            current = delivery_store._load_sync(delivery_id)
            if current.output_batch_id != output_batch_id:
                continue
            if current.state.value not in {"selected", "failed"}:
                raise RuntimeError(
                    "IR-7 stale output owns a non-cancellable artifact attempt"
                )
            updated = ArtifactDeliveryRecord.model_validate(
                current.model_copy(
                    update={"output_batch_id": None}
                ).model_dump(mode="python")
            )
            updates[delivery_id] = (updated, True)
        delivery_store._commit_batch_sync(updates)

        if batch is not None:
            output_store._rollback_new_commit_sync(batch)
