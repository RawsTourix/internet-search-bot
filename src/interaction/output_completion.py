"""Rollback-aware aggregate completion across artifact and OutputBatch stores."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from ..artifacts.delivery import FileSystemArtifactDeliveryStore
from ..artifacts.errors import ArtifactStorageError
from ..artifacts.models import ArtifactDeliveryState
from .errors import OutputBatchConflictError
from .output_models import (
    ArtifactOutputPart,
    OutputBatch,
    OutputDeliveryReceipt,
    OutputPartReceiptState,
)
from .output_store import FileSystemOutputBatchStore


class OutputDeliveryCompletionService:
    """Commit all part facts and aggregate state or restore every file."""

    def __init__(
        self,
        *,
        output_store: FileSystemOutputBatchStore,
        artifact_delivery_store: FileSystemArtifactDeliveryStore,
    ) -> None:
        self.output_store = output_store
        self.artifact_delivery_store = artifact_delivery_store
        self._lock = threading.RLock()

    async def complete(
        self,
        receipt: OutputDeliveryReceipt,
    ) -> OutputBatch:
        return await asyncio.to_thread(self._complete_sync, receipt)

    def _complete_sync(
        self,
        receipt: OutputDeliveryReceipt,
    ) -> OutputBatch:
        with self._lock:
            batch = self.output_store._load_sync(receipt.output_batch_id)
            part_by_id = {part.part_id: part for part in batch.parts}
            expected = [(part.part_id, part.index) for part in batch.parts]
            received = [
                (part.part_id, part.index)
                for part in receipt.part_receipts
            ]
            if received != expected:
                raise OutputBatchConflictError(
                    "aggregate receipt does not match committed output parts"
                )

            delivery_updates: list[
                tuple[str, ArtifactDeliveryState, set[ArtifactDeliveryState], str | None, dict]
            ] = []
            for part_receipt in receipt.part_receipts:
                part = part_by_id[part_receipt.part_id]
                exact_delivery_id = getattr(part, "delivery_id", None)
                if part_receipt.delivery_id != exact_delivery_id:
                    raise OutputBatchConflictError(
                        "part receipt delivery identity mismatch"
                    )
                if not isinstance(part, ArtifactOutputPart):
                    continue
                record = self.artifact_delivery_store._load_sync(
                    part.delivery_id
                )
                if (
                    record.session_id != batch.session_id
                    or record.cycle_id != batch.cycle_id
                    or record.artifact_id != part.artifact_id
                ):
                    raise OutputBatchConflictError(
                        "artifact delivery is outside OutputBatch authority"
                    )
                target, allowed = self._artifact_transition(
                    part_receipt.state
                )
                delivery_updates.append(
                    (
                        part.delivery_id,
                        target,
                        allowed,
                        part_receipt.error_category,
                        {
                            "provider": "telegram",
                            "message_ids": list(
                                part_receipt.client_message_ids
                            ),
                            "output_batch_id": batch.output_batch_id,
                            "output_part_id": part.part_id,
                            "output_part_index": part.index,
                        },
                    )
                )

            paths = [
                self.output_store.records
                / batch.output_batch_id
                / "state.json",
                self.output_store.attempts
                / f"{receipt.attempt_id}.json",
                *[
                    self.artifact_delivery_store.root
                    / f"{delivery_id}.json"
                    for delivery_id, *_ in delivery_updates
                ],
            ]
            backups = self._backups(paths)
            try:
                for (
                    delivery_id,
                    target,
                    allowed,
                    error,
                    transport_receipt,
                ) in delivery_updates:
                    self.artifact_delivery_store._transition_sync(
                        delivery_id,
                        target,
                        allowed,
                        error,
                        transport_receipt,
                    )
                return self.output_store._complete_sync(receipt)
            except BaseException:
                self._restore(backups)
                raise

    @staticmethod
    def _artifact_transition(
        state: OutputPartReceiptState,
    ) -> tuple[ArtifactDeliveryState, set[ArtifactDeliveryState]]:
        if state == OutputPartReceiptState.DELIVERED:
            return (
                ArtifactDeliveryState.DELIVERED,
                {ArtifactDeliveryState.DELIVERING},
            )
        if state == OutputPartReceiptState.UNKNOWN:
            return (
                ArtifactDeliveryState.UNKNOWN,
                {ArtifactDeliveryState.DELIVERING},
            )
        return (
            ArtifactDeliveryState.FAILED,
            {
                ArtifactDeliveryState.SELECTED,
                ArtifactDeliveryState.DELIVERING,
            },
        )

    @staticmethod
    def _backups(paths: list[Path]) -> dict[Path, bytes | None]:
        result: dict[Path, bytes | None] = {}
        try:
            for path in dict.fromkeys(paths):
                if path.is_symlink():
                    raise ArtifactStorageError(
                        "unsafe path in aggregate completion"
                    )
                result[path] = path.read_bytes() if path.exists() else None
        except OSError as error:
            raise ArtifactStorageError(
                "failed to prepare aggregate completion"
            ) from error
        return result

    @staticmethod
    def _restore(backups: dict[Path, bytes | None]) -> None:
        try:
            for path, payload in backups.items():
                if payload is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(payload)
        except OSError as error:
            raise ArtifactStorageError(
                "failed to roll back aggregate completion"
            ) from error
