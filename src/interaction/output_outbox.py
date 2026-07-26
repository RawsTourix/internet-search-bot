"""Bounded transport-owned projection of safe-to-start OutputBatch records."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .output_models import OutputBatch, OutputBatchKind, OutputBatchState
from .output_store import FileSystemOutputBatchStore


class ReadyOutputOutboxService:
    """Expose only delivery attempts that are still safe to start.

    The filesystem implementation scans the v0.4 store and returns a bounded
    projection. A future database/queue repository can implement the same
    application contract without changing transport workers.
    """

    MAX_LIMIT = 500
    MAX_MINIMUM_AGE_SECONDS = 3600.0

    def __init__(self, store: FileSystemOutputBatchStore) -> None:
        self.store = store

    async def list_ready(
        self,
        *,
        client_type: str,
        client_instance_id: str,
        kind: OutputBatchKind = OutputBatchKind.FINAL,
        limit: int = 50,
        minimum_age_seconds: float = 30.0,
        now: datetime | None = None,
    ) -> list[OutputBatch]:
        normalized_client = self._required(client_type, "client_type")
        normalized_instance = self._required(
            client_instance_id,
            "client_instance_id",
        )
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("ready outbox limit must be an integer")
        if limit <= 0 or limit > self.MAX_LIMIT:
            raise ValueError(
                f"ready outbox limit must be between 1 and {self.MAX_LIMIT}"
            )
        if (
            isinstance(minimum_age_seconds, bool)
            or not isinstance(minimum_age_seconds, (int, float))
            or minimum_age_seconds < 0
            or minimum_age_seconds > self.MAX_MINIMUM_AGE_SECONDS
        ):
            raise ValueError(
                "ready outbox minimum age must be between 0 and "
                f"{self.MAX_MINIMUM_AGE_SECONDS:g} seconds"
            )
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise ValueError("ready outbox clock must be timezone-aware")
        ready_before = current_time.astimezone(timezone.utc) - timedelta(
            seconds=float(minimum_age_seconds)
        )

        candidates = await self.store.list_recoverable()
        ready = [
            batch
            for batch in candidates
            if batch.state == OutputBatchState.READY
            and batch.kind == kind
            and (batch.ready_at or batch.created_at) <= ready_before
            and batch.capability_snapshot.client_type == normalized_client
            and (
                batch.capability_snapshot.client_instance_id
                == normalized_instance
            )
        ]
        ready.sort(
            key=lambda batch: (
                batch.ready_at or batch.created_at,
                batch.sequence_number,
                batch.output_batch_id,
            )
        )
        return ready[:limit]

    @staticmethod
    def validate_authority(
        batch: OutputBatch,
        *,
        session_id: str,
        client_type: str,
        client_instance_id: str,
    ) -> None:
        if batch.session_id != session_id.strip():
            raise PermissionError("Output batch session authority mismatch")
        if batch.capability_snapshot.client_type != client_type.strip():
            raise PermissionError("Output batch client authority mismatch")
        if (
            batch.capability_snapshot.client_instance_id
            != client_instance_id.strip()
        ):
            raise PermissionError("Output batch client instance authority mismatch")

    @staticmethod
    def _required(value: str, field_name: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{field_name} must not be empty")
        return normalized
