"""Bounded transport-owned projection of safe-to-start OutputBatch records."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..runtime.finalization_bridge import output_delivery_allowed
from .errors import OutputBatchConflictError
from .ids import is_interaction_id
from .output_models import OutputBatch, OutputBatchKind, OutputBatchState
from .output_store import FileSystemOutputBatchStore


class ReadyOutputOutboxRef(BaseModel):
    """Minimal transport polling reference; the manifest remains behind claim."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    output_batch_id: str
    session_id: str
    cycle_id: str
    sequence_number: int = Field(ge=1)
    kind: OutputBatchKind
    client_type: str
    client_instance_id: str
    ready_at: datetime

    @field_validator("output_batch_id")
    @classmethod
    def validate_output_batch_id(cls, value: str) -> str:
        if not is_interaction_id(value, prefix="obat"):
            raise ValueError("invalid ready outbox output batch ID")
        return value

    @field_validator(
        "session_id",
        "cycle_id",
        "client_type",
        "client_instance_id",
    )
    @classmethod
    def validate_required(cls, value: str, info) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{info.field_name} must not be empty")
        return normalized

    @field_validator("ready_at")
    @classmethod
    def normalize_ready_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("ready outbox timestamp must be timezone-aware")
        return value.astimezone(timezone.utc)

    @classmethod
    def from_batch(cls, batch: OutputBatch) -> "ReadyOutputOutboxRef":
        return cls(
            output_batch_id=batch.output_batch_id,
            session_id=batch.session_id,
            cycle_id=batch.cycle_id,
            sequence_number=batch.sequence_number,
            kind=batch.kind,
            client_type=batch.capability_snapshot.client_type,
            client_instance_id=batch.capability_snapshot.client_instance_id,
            ready_at=batch.ready_at or batch.created_at,
        )


class ReadyOutputOutboxService:
    """Expose only delivery attempts that are still safe to start.

    READY persistence is not delivery authority for a final aggregate. IR-7
    therefore filters final records through the durable terminal-commit gate.
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
    ) -> list[ReadyOutputOutboxRef]:
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
        ready: list[OutputBatch] = []
        for batch in candidates:
            if (
                batch.state != OutputBatchState.READY
                or batch.kind != kind
                or (batch.ready_at or batch.created_at) > ready_before
                or batch.capability_snapshot.client_type != normalized_client
                or batch.capability_snapshot.client_instance_id
                != normalized_instance
            ):
                continue
            if not await output_delivery_allowed(batch):
                continue
            ready.append(batch)
        ready.sort(
            key=lambda batch: (
                batch.ready_at or batch.created_at,
                batch.sequence_number,
                batch.output_batch_id,
            )
        )
        return [ReadyOutputOutboxRef.from_batch(batch) for batch in ready[:limit]]

    @staticmethod
    def validate_authority(
        batch: OutputBatch,
        *,
        session_id: str,
        client_type: str,
        client_instance_id: str,
    ) -> None:
        if batch.kind != OutputBatchKind.FINAL:
            raise OutputBatchConflictError(
                "Recovery outbox can claim only final OutputBatch records"
            )
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
