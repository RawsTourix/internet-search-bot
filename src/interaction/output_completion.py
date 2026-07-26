"""Rollback-aware aggregate completion across artifact and OutputBatch stores."""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..artifacts.delivery import FileSystemArtifactDeliveryStore
from ..artifacts.errors import ArtifactStorageError
from ..artifacts.models import ArtifactDeliveryState
from .errors import OutputBatchConflictError
from .output_models import (
    ArtifactOutputPart,
    OutputBatch,
    OutputBatchState,
    OutputDeliveryReceipt,
    OutputDeliveryReceiptState,
    OutputPartReceipt,
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
        bind_reconciliation = getattr(
            self.output_store,
            "bind_reconciliation_handler",
            None,
        )
        if bind_reconciliation is not None:
            bind_reconciliation(self.reconcile_unknown)
        bind_stale_recovery = getattr(
            self.output_store,
            "bind_stale_recovery_handler",
            None,
        )
        if bind_stale_recovery is not None:
            bind_stale_recovery(self.recover_stale_claims)

    async def complete(
        self,
        receipt: OutputDeliveryReceipt,
    ) -> OutputBatch:
        return await asyncio.to_thread(
            self._apply_sync,
            receipt,
            False,
        )

    async def reconcile_unknown(
        self,
        receipt: OutputDeliveryReceipt,
    ) -> OutputBatch:
        """Resolve one unknown attempt across OutputBatch and artifact stores."""
        return await asyncio.to_thread(
            self._apply_sync,
            receipt,
            True,
        )

    async def recover_stale_claims(
        self,
        *,
        timeout_seconds: int,
        now: datetime | None = None,
    ) -> list[OutputBatch]:
        """Conservatively finish stale claims across both durable stores."""
        return await asyncio.to_thread(
            self._recover_stale_claims_sync,
            timeout_seconds,
            now or datetime.now(timezone.utc),
        )

    def _recover_stale_claims_sync(
        self,
        timeout_seconds: int,
        now: datetime,
    ) -> list[OutputBatch]:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive integer")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current_time = now.astimezone(timezone.utc)
        deadline = current_time - timedelta(seconds=timeout_seconds)
        recovered: list[OutputBatch] = []

        # Reading and applying are both protected by the same ordered store locks.
        # A concurrent live completion either wins before this block or observes
        # the terminal state afterwards; stale recovery never blind-resends.
        with (
            self._lock,
            self.output_store._lock,
            self.artifact_delivery_store._lock,
        ):
            for batch in self.output_store._list_recoverable_sync():
                if batch.state != OutputBatchState.DELIVERING:
                    continue
                state_path = (
                    self.output_store.records
                    / batch.output_batch_id
                    / "state.json"
                )
                state = self.output_store._read(state_path)
                updated_at = datetime.fromisoformat(str(state["updated_at"]))
                if updated_at.tzinfo is None or updated_at.utcoffset() is None:
                    raise OutputBatchConflictError(
                        "output state timestamp must be timezone-aware"
                    )
                if updated_at.astimezone(timezone.utc) > deadline:
                    continue
                attempt_id = str(state.get("attempt_id") or "")
                receipt = self._build_stale_receipt(
                    batch=batch,
                    attempt_id=attempt_id,
                    started_at=updated_at.astimezone(timezone.utc),
                    completed_at=current_time,
                )
                recovered.append(self._apply_sync(receipt, False))
        return recovered

    def _build_stale_receipt(
        self,
        *,
        batch: OutputBatch,
        attempt_id: str,
        started_at: datetime,
        completed_at: datetime,
    ) -> OutputDeliveryReceipt:
        part_receipts: list[OutputPartReceipt] = []
        for part in batch.parts:
            if not isinstance(part, ArtifactOutputPart):
                part_receipts.append(
                    OutputPartReceipt(
                        part_id=part.part_id,
                        index=part.index,
                        required=part.required,
                        state=OutputPartReceiptState.UNKNOWN,
                        error_category="delivery_claim_timeout_after_start",
                    )
                )
                continue

            record = self.artifact_delivery_store._load_sync(part.delivery_id)
            if (
                record.session_id != batch.session_id
                or record.cycle_id != batch.cycle_id
                or record.artifact_id != part.artifact_id
            ):
                raise OutputBatchConflictError(
                    "artifact delivery is outside OutputBatch authority"
                )
            if record.state == ArtifactDeliveryState.SELECTED:
                part_state = OutputPartReceiptState.FAILED
                error_category = "transport_not_started_before_recovery"
                message_ids: tuple[str, ...] = ()
                delivered_at = None
            elif record.state in {
                ArtifactDeliveryState.DELIVERING,
                ArtifactDeliveryState.UNKNOWN,
            }:
                part_state = OutputPartReceiptState.UNKNOWN
                error_category = "delivery_claim_timeout_after_start"
                message_ids = tuple(
                    str(item)
                    for item in record.receipt.get("message_ids", [])
                )
                delivered_at = None
            elif record.state == ArtifactDeliveryState.FAILED:
                part_state = OutputPartReceiptState.FAILED
                error_category = record.last_error or "artifact_delivery_failed"
                message_ids = ()
                delivered_at = None
            elif record.state == ArtifactDeliveryState.DELIVERED:
                message_ids = tuple(
                    str(item)
                    for item in record.receipt.get("message_ids", [])
                )
                if not message_ids:
                    raise OutputBatchConflictError(
                        "delivered artifact recovery lacks exact client message IDs"
                    )
                part_state = OutputPartReceiptState.DELIVERED
                error_category = None
                delivered_at = record.delivered_at or completed_at
            else:
                raise OutputBatchConflictError(
                    "cancelled artifact cannot belong to an active OutputBatch"
                )
            part_receipts.append(
                OutputPartReceipt(
                    part_id=part.part_id,
                    index=part.index,
                    required=part.required,
                    state=part_state,
                    delivery_id=part.delivery_id,
                    client_message_ids=message_ids,
                    error_category=error_category,
                    delivered_at=delivered_at,
                )
            )

        return OutputDeliveryReceipt(
            output_batch_id=batch.output_batch_id,
            attempt_id=attempt_id,
            state=self._aggregate_state(tuple(part_receipts)),
            part_receipts=tuple(part_receipts),
            started_at=started_at,
            completed_at=completed_at,
        )

    def _apply_sync(
        self,
        receipt: OutputDeliveryReceipt,
        reconciling: bool,
    ) -> OutputBatch:
        # The transaction lock is intentionally shared with the stores' own
        # mutation locks. All process-local writers serialize against backup,
        # commit and rollback, preventing a rollback from erasing a concurrent
        # valid mutation.
        with (
            self._lock,
            self.output_store._lock,
            self.artifact_delivery_store._lock,
        ):
            batch = self.output_store._load_sync(receipt.output_batch_id)
            part_by_id = {part.part_id: part for part in batch.parts}
            expected = [
                (part.part_id, part.index, part.required)
                for part in batch.parts
            ]
            received = [
                (part.part_id, part.index, part.required)
                for part in receipt.part_receipts
            ]
            if received != expected:
                raise OutputBatchConflictError(
                    "aggregate receipt does not match committed output parts"
                )

            delivery_updates: list[
                tuple[
                    str,
                    ArtifactDeliveryState,
                    set[ArtifactDeliveryState],
                    str | None,
                    dict,
                ]
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
                    part_receipt.state,
                    reconciling=reconciling,
                )
                delivery_updates.append(
                    (
                        part.delivery_id,
                        target,
                        allowed,
                        part_receipt.error_category,
                        {
                            "provider": batch.capability_snapshot.client_type,
                            "client_instance_id": (
                                batch.capability_snapshot.client_instance_id
                            ),
                            "message_ids": list(
                                part_receipt.client_message_ids
                            ),
                            "output_batch_id": batch.output_batch_id,
                            "output_part_id": part.part_id,
                            "output_part_index": part.index,
                            "reconciled": reconciling,
                        },
                    )
                )

            attempt_path = (
                self.output_store.attempts
                / (
                    f"{receipt.attempt_id}.reconciled.json"
                    if reconciling
                    else f"{receipt.attempt_id}.json"
                )
            )
            paths = [
                self.output_store.records
                / batch.output_batch_id
                / "state.json",
                attempt_path,
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
                if reconciling:
                    return self.output_store._reconcile_unknown_sync(receipt)
                return self.output_store._complete_sync(receipt)
            except BaseException:
                self._restore(backups)
                raise

    @staticmethod
    def _artifact_transition(
        state: OutputPartReceiptState,
        *,
        reconciling: bool,
    ) -> tuple[ArtifactDeliveryState, set[ArtifactDeliveryState]]:
        if reconciling:
            if state == OutputPartReceiptState.DELIVERED:
                return (
                    ArtifactDeliveryState.DELIVERED,
                    {ArtifactDeliveryState.UNKNOWN},
                )
            if state in {
                OutputPartReceiptState.FAILED,
                OutputPartReceiptState.SKIPPED,
            }:
                return (
                    ArtifactDeliveryState.FAILED,
                    {ArtifactDeliveryState.UNKNOWN},
                )
            raise OutputBatchConflictError(
                "artifact reconciliation requires a confirmed delivered or failed outcome"
            )

        if state == OutputPartReceiptState.DELIVERED:
            return (
                ArtifactDeliveryState.DELIVERED,
                {ArtifactDeliveryState.DELIVERING},
            )
        if state in {
            OutputPartReceiptState.UNKNOWN,
            OutputPartReceiptState.PARTIALLY_DELIVERED,
        }:
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
    def _aggregate_state(
        receipts: tuple[OutputPartReceipt, ...],
    ) -> OutputDeliveryReceiptState:
        if any(
            item.state == OutputPartReceiptState.UNKNOWN
            for item in receipts
        ):
            return OutputDeliveryReceiptState.UNKNOWN
        required = [item for item in receipts if item.required] or list(receipts)
        if required and all(
            item.state == OutputPartReceiptState.DELIVERED
            for item in required
        ):
            return OutputDeliveryReceiptState.DELIVERED
        if any(
            item.state
            in {
                OutputPartReceiptState.DELIVERED,
                OutputPartReceiptState.PARTIALLY_DELIVERED,
            }
            for item in required
        ):
            return OutputDeliveryReceiptState.PARTIALLY_DELIVERED
        return OutputDeliveryReceiptState.FAILED

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
