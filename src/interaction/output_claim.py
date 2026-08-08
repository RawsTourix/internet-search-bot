"""Rollback-aware idempotency boundary for OutputBatch delivery claims."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from ..runtime.finalization_bridge import output_delivery_allowed
from .errors import (
    InteractionIntegrityError,
    InteractionStorageError,
    OutputBatchConflictError,
)
from .ids import is_interaction_id
from .output_models import OutputBatch, OutputBatchState
from .output_store import FileSystemOutputBatchStore


class IdempotentOutputClaimService:
    """Persist one stable request key for a durable OutputBatch claim.

    A retry with the same request ID receives the original attempt. Another
    request ID cannot join an active claim. New READY final claims additionally
    require IR-7 terminal authority; an already-started DELIVERING replay keeps
    the pre-existing transport attempt.
    """

    def __init__(self, store: FileSystemOutputBatchStore) -> None:
        self.store = store
        self.requests = self.store.root / "claim_requests"
        try:
            self.requests.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise InteractionStorageError(
                "failed to initialize output claim request storage"
            ) from error

    async def claim(
        self,
        output_batch_id: str,
        *,
        claim_request_id: str,
        now: datetime | None = None,
    ) -> tuple[OutputBatch, str]:
        current = await self.store.get(output_batch_id)
        if (
            current.state == OutputBatchState.READY
            and not await output_delivery_allowed(current)
        ):
            raise OutputBatchConflictError(
                "final OutputBatch is not terminal-committed"
            )
        return await asyncio.to_thread(
            self._claim_sync,
            output_batch_id,
            claim_request_id,
            now or datetime.now(timezone.utc),
        )

    def _claim_sync(
        self,
        output_batch_id: str,
        claim_request_id: str,
        now: datetime,
    ) -> tuple[OutputBatch, str]:
        if not is_interaction_id(claim_request_id, prefix="oclm"):
            raise ValueError("invalid output claim request ID")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("output claim timestamp must be timezone-aware")
        request_path = self.requests / f"{claim_request_id}.json"
        state_path = self.store.records / output_batch_id / "state.json"

        with self.store._lock:
            if request_path.is_symlink():
                raise InteractionIntegrityError(
                    "unsafe output claim request metadata path"
                )
            if request_path.exists():
                request = self.store._read(request_path)
                if request.get("output_batch_id") != output_batch_id:
                    raise OutputBatchConflictError(
                        "output claim request ID was reused for another batch"
                    )
                attempt_id = str(request.get("attempt_id") or "")
                current = self.store._load_sync(output_batch_id)
                state = self.store._read(state_path)
                if (
                    current.state != OutputBatchState.DELIVERING
                    or state.get("attempt_id") != attempt_id
                ):
                    raise OutputBatchConflictError(
                        "output claim replay no longer owns an active attempt"
                    )
                return current, attempt_id

            try:
                previous_state = state_path.read_bytes()
            except OSError as error:
                raise InteractionStorageError(
                    "failed to prepare output claim transaction"
                ) from error

            try:
                claimed, attempt_id = self.store._claim_sync(
                    output_batch_id,
                    now.astimezone(timezone.utc),
                )
                self.store._write(
                    request_path,
                    {
                        "claim_request_id": claim_request_id,
                        "output_batch_id": output_batch_id,
                        "attempt_id": attempt_id,
                        "claimed_at": now.astimezone(timezone.utc).isoformat(),
                    },
                )
                return claimed, attempt_id
            except BaseException:
                rollback_error: BaseException | None = None
                try:
                    request_path.unlink(missing_ok=True)
                    state_path.write_bytes(previous_state)
                except OSError as error:
                    rollback_error = error
                if rollback_error is not None:
                    raise InteractionStorageError(
                        "failed to roll back output claim transaction"
                    ) from rollback_error
                raise
