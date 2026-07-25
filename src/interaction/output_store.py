"""Filesystem OutputBatch store with immutable manifests and mutable receipts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .errors import (
    InteractionIntegrityError,
    InteractionStorageError,
    OutputBatchConflictError,
    OutputBatchNotFoundError,
)
from .ids import (
    is_interaction_id,
    new_output_attempt_id,
    new_output_batch_id,
)
from .output_models import (
    OutputBatch,
    OutputBatchKind,
    OutputBatchState,
    OutputDeliveryReceipt,
    OutputDeliveryReceiptState,
    OutputPartReceipt,
    OutputPartReceiptState,
)

logger = logging.getLogger("Interaction.OutputStore")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


class FileSystemOutputBatchStore:
    """Commit-once manifests and explicit delivery state transitions."""

    _ALLOWED = {
        OutputBatchState.READY: {
            OutputBatchState.DELIVERING,
            OutputBatchState.CANCELLED,
        },
        OutputBatchState.DELIVERING: {
            OutputBatchState.DELIVERED,
            OutputBatchState.PARTIALLY_DELIVERED,
            OutputBatchState.FAILED,
        },
    }

    def __init__(self, root: Path, *, atomic_writes: bool = True) -> None:
        self.root = Path(root) / "output_batches"
        self.records = self.root / "records"
        self.cycle_index = self.root / "by_cycle"
        self.attempts = self.root / "attempts"
        self.atomic_writes = atomic_writes
        self._lock = threading.RLock()
        for path in (self.records, self.cycle_index, self.attempts):
            path.mkdir(parents=True, exist_ok=True)

    async def commit(self, batch: OutputBatch) -> tuple[OutputBatch, bool]:
        return await asyncio.to_thread(self._commit_sync, batch)

    def _commit_sync(self, batch: OutputBatch) -> tuple[OutputBatch, bool]:
        with self._lock:
            if batch.state != OutputBatchState.READY:
                raise OutputBatchConflictError("only ready output batches can commit")
            stable = batch.model_dump(mode="json", exclude={"state", "completed_at"})
            identity = self._identity(batch.session_id, batch.cycle_id, batch.kind)
            index_path = self.cycle_index / f"{identity}.json"
            fingerprint_payload = dict(stable)
            for generated in (
                "output_batch_id",
                "created_at",
                "ready_at",
            ):
                fingerprint_payload.pop(generated, None)
            fingerprint_payload["parts"] = [
                {
                    key: value
                    for key, value in part.items()
                    if key != "part_id"
                }
                for part in fingerprint_payload.get("parts", [])
            ]
            fingerprint = _fingerprint(fingerprint_payload)
            if index_path.exists():
                pointer = self._read(index_path)
                existing = self._load_sync(pointer["output_batch_id"])
                if pointer["fingerprint"] != fingerprint:
                    raise OutputBatchConflictError(
                        "output identity reused with different semantic output"
                    )
                return existing, False

            batch_dir = self.records / batch.output_batch_id
            if batch_dir.exists():
                raise OutputBatchConflictError("output batch ID already exists")
            batch_dir.mkdir(parents=True)
            self._write(batch_dir / "manifest.json", stable)
            self._write(
                batch_dir / "state.json",
                {
                    "state": batch.state.value,
                    "completed_at": None,
                    "updated_at": batch.ready_at.isoformat()
                    if batch.ready_at
                    else batch.created_at.isoformat(),
                    "attempt_id": None,
                },
            )
            self._write(
                index_path,
                {
                    "output_batch_id": batch.output_batch_id,
                    "fingerprint": fingerprint,
                },
            )
            logger.info(
                "output_batch_created output_batch_id=%s session_id=%s "
                "cycle_id=%s part_count=%s",
                batch.output_batch_id,
                batch.session_id,
                batch.cycle_id,
                len(batch.parts),
            )
            return batch, True

    async def get(self, output_batch_id: str) -> OutputBatch:
        return await asyncio.to_thread(self._load_sync, output_batch_id)

    async def claim_delivery(
        self, output_batch_id: str, *, now: datetime | None = None
    ) -> tuple[OutputBatch, str]:
        return await asyncio.to_thread(
            self._claim_sync, output_batch_id, now or utc_now()
        )

    def _claim_sync(
        self, output_batch_id: str, now: datetime
    ) -> tuple[OutputBatch, str]:
        with self._lock:
            current = self._load_sync(output_batch_id)
            state_path = self.records / output_batch_id / "state.json"
            state = self._read(state_path)
            if current.state == OutputBatchState.DELIVERING:
                raise OutputBatchConflictError(
                    "output batch already has an active delivery claim"
                )
            if current.state != OutputBatchState.READY:
                raise OutputBatchConflictError("output batch cannot be claimed")
            attempt_id = new_output_attempt_id()
            state.update(
                state=OutputBatchState.DELIVERING.value,
                updated_at=now.isoformat(),
                attempt_id=attempt_id,
            )
            self._write(state_path, state)
            logger.info(
                "output_batch_delivery_started output_batch_id=%s attempt_id=%s",
                output_batch_id,
                attempt_id,
            )
            return self._load_sync(output_batch_id), attempt_id

    async def complete(
        self,
        receipt: OutputDeliveryReceipt,
    ) -> OutputBatch:
        return await asyncio.to_thread(self._complete_sync, receipt)

    def _complete_sync(self, receipt: OutputDeliveryReceipt) -> OutputBatch:
        from .output_models import OutputDeliveryReceiptState

        mapped = {
            OutputDeliveryReceiptState.DELIVERED: OutputBatchState.DELIVERED,
            OutputDeliveryReceiptState.PARTIALLY_DELIVERED:
                OutputBatchState.PARTIALLY_DELIVERED,
            OutputDeliveryReceiptState.FAILED: OutputBatchState.FAILED,
            # Unknown must remain visible and must not trigger an automatic retry.
            OutputDeliveryReceiptState.UNKNOWN: OutputBatchState.FAILED,
        }[receipt.state]
        with self._lock:
            current = self._load_sync(receipt.output_batch_id)
            expected_parts = [
                (item.part_id, item.index) for item in current.parts
            ]
            received_parts = [
                (item.part_id, item.index) for item in receipt.part_receipts
            ]
            if received_parts != expected_parts:
                raise OutputBatchConflictError(
                    "delivery receipt does not match committed output parts"
                )
            state_path = self.records / receipt.output_batch_id / "state.json"
            state = self._read(state_path)
            if current.state in {
                OutputBatchState.DELIVERED,
                OutputBatchState.PARTIALLY_DELIVERED,
                OutputBatchState.FAILED,
            }:
                existing_path = self.attempts / f"{receipt.attempt_id}.json"
                if existing_path.exists():
                    existing = OutputDeliveryReceipt.model_validate(
                        self._read(existing_path)
                    )
                    if existing != receipt:
                        raise OutputBatchConflictError("receipt replay conflicts")
                return current
            if current.state != OutputBatchState.DELIVERING:
                raise OutputBatchConflictError("output batch is not delivering")
            if state.get("attempt_id") != receipt.attempt_id:
                raise OutputBatchConflictError("delivery attempt does not own claim")
            self._write(
                self.attempts / f"{receipt.attempt_id}.json",
                receipt.model_dump(mode="json"),
            )
            state.update(
                state=mapped.value,
                completed_at=receipt.completed_at.isoformat(),
                updated_at=receipt.completed_at.isoformat(),
            )
            self._write(state_path, state)
            logger.info(
                "%s output_batch_id=%s attempt_id=%s part_count=%s",
                {
                    OutputBatchState.DELIVERED: "output_batch_delivered",
                    OutputBatchState.PARTIALLY_DELIVERED:
                        "output_batch_partially_delivered",
                    OutputBatchState.FAILED: "output_batch_failed",
                }[mapped],
                receipt.output_batch_id,
                receipt.attempt_id,
                len(receipt.part_receipts),
            )
            return self._load_sync(receipt.output_batch_id)

    async def list_recoverable(self) -> list[OutputBatch]:
        return await asyncio.to_thread(self._list_recoverable_sync)

    async def reconcile_stale_claims(
        self,
        *,
        timeout_seconds: int,
        now: datetime | None = None,
    ) -> list[OutputBatch]:
        return await asyncio.to_thread(
            self._reconcile_stale_claims_sync,
            timeout_seconds,
            now or utc_now(),
        )

    def _reconcile_stale_claims_sync(
        self,
        timeout_seconds: int,
        now: datetime,
    ) -> list[OutputBatch]:
        reconciled: list[OutputBatch] = []
        deadline = now - timedelta(seconds=timeout_seconds)
        with self._lock:
            for batch in self._list_recoverable_sync():
                if batch.state != OutputBatchState.DELIVERING:
                    continue
                state = self._read(
                    self.records / batch.output_batch_id / "state.json"
                )
                updated_at = datetime.fromisoformat(str(state["updated_at"]))
                if updated_at.tzinfo is None or updated_at.utcoffset() is None:
                    raise InteractionIntegrityError(
                        "output state timestamp must be timezone-aware"
                    )
                if updated_at.astimezone(timezone.utc) > deadline:
                    continue
                attempt_id = str(state.get("attempt_id") or "")
                receipt = OutputDeliveryReceipt(
                    output_batch_id=batch.output_batch_id,
                    attempt_id=attempt_id,
                    state=OutputDeliveryReceiptState.UNKNOWN,
                    part_receipts=tuple(
                        OutputPartReceipt(
                            part_id=part.part_id,
                            index=part.index,
                            state=OutputPartReceiptState.UNKNOWN,
                            delivery_id=getattr(part, "delivery_id", None),
                            error_category="delivery_claim_timeout_after_start",
                        )
                        for part in batch.parts
                    ),
                    started_at=updated_at,
                    completed_at=now,
                )
                reconciled.append(self._complete_sync(receipt))
        return reconciled

    def _list_recoverable_sync(self) -> list[OutputBatch]:
        result: list[OutputBatch] = []
        for path in sorted(self.records.glob("obat_*")):
            if not path.is_dir() or path.is_symlink():
                continue
            item = self._load_sync(path.name)
            # DELIVERING is reconciled, never blindly re-sent.
            if item.state in {OutputBatchState.READY, OutputBatchState.DELIVERING}:
                result.append(item)
        return result

    @staticmethod
    def _identity(
        session_id: str, cycle_id: str, kind: OutputBatchKind
    ) -> str:
        raw = f"{session_id}\0{cycle_id}\0{kind.value}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _load_sync(self, output_batch_id: str) -> OutputBatch:
        if not is_interaction_id(output_batch_id, prefix="obat"):
            raise OutputBatchNotFoundError("invalid output batch ID")
        batch_dir = self.records / output_batch_id
        if not batch_dir.exists():
            raise OutputBatchNotFoundError("output batch does not exist")
        if batch_dir.is_symlink():
            raise InteractionIntegrityError("symlink output batch is not allowed")
        manifest = self._read(batch_dir / "manifest.json")
        state = self._read(batch_dir / "state.json")
        payload = dict(manifest)
        payload["state"] = state["state"]
        payload["completed_at"] = state.get("completed_at")
        try:
            return OutputBatch.model_validate(payload)
        except Exception as error:
            raise InteractionIntegrityError("invalid stored OutputBatch") from error

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        try:
            if path.is_symlink():
                raise InteractionIntegrityError("symlink metadata is not allowed")
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("metadata root must be an object")
            return payload
        except InteractionIntegrityError:
            raise
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise InteractionStorageError("failed to read output metadata") from error

    def _write(self, path: Path, payload: dict[str, Any]) -> None:
        data = _canonical(payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if not self.atomic_writes:
                path.write_bytes(data)
                return
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                temporary.write_bytes(data)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        except OSError as error:
            raise InteractionStorageError("failed to persist output metadata") from error


def build_ready_output_batch(**values: Any) -> OutputBatch:
    """Convenience constructor that centralizes ready timestamps and IDs."""
    now = values.pop("now", None) or utc_now()
    return OutputBatch(
        output_batch_id=values.pop("output_batch_id", new_output_batch_id()),
        state=OutputBatchState.READY,
        created_at=now,
        ready_at=now,
        completed_at=None,
        **values,
    )
